#!/usr/bin/env python

import json
import datetime
import uuid
from random import randint
import boto3
import os

from states import State

aws_region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# Encapsulate global state and functionality in a class
class ServerState:
    def __init__(self):
        """Initialize the state variables."""
        self.my_state = State.INITIALIZING
        self.prompts = []
        self.current_prompt_index = 0
        self.my_prompt = None
        self.my_model = None
        self.my_result_a = ""
        self.my_result_b = ""
        self.my_human_preference = None
        self.button_pressed = False
        self.my_task = None
        self.my_error = None
        self.my_error_time = datetime.datetime.now().timestamp()
        self.my_last_interaction_time = datetime.datetime.now().timestamp()
        self.my_uuid = uuid.uuid4()

        self.bedrock_runtime = boto3.client(
            service_name="bedrock-runtime",
            region_name=aws_region
        )

        self.load_prompts("prompts.json")
        self.get_next_prompt()

    def load_prompts(self, file_path):
        """Load prompts from a given JSON file."""
        try:
            with open(file_path, 'r') as f:
                self.prompts = json.load(f)
            print(f"Found {len(self.prompts)} prompts.")
        except Exception as e:
            print(f"An error occurred while loading prompts: {e}")
            self.my_state = State.ERROR
    
    def get_next_prompt(self):
        """Called at the end of a human interaction to get a new prompt and 
        reset the state variables."""
        self.my_result_a = ""
        self.my_result_b = ""
        self.my_human_preference = None
        self.button_pressed = False
        self.my_state = State.TRANSCRIBING
        self.my_uuid = uuid.uuid4()
        if self.prompts:
            if self.current_prompt_index >= len(self.prompts):
                self.current_prompt_index = 0  # Loop back to the start
            self.my_prompt = self.prompts[self.current_prompt_index]["prompt"]
            self.my_model = self.prompts[self.current_prompt_index]["model"]
            self.current_prompt_index += 1  # Increment the index for the next call

        else:
            print("No prompts are loaded.")
            return None
    
    def red_button_callback(self, channel):
        """Logic for handling a red button press."""
        self.my_human_preference = 'a'
        self.my_last_interaction_time = datetime.datetime.now().timestamp()
        print("Red button pressed!") 
        self.button_pressed = True
        if self.my_model == "claude":
            self.my_state = State.SELECT_A_TXT
        elif self.my_model == "sdxl":
            self.my_state = State.SELECT_A_IMG
        else:
            raise ValueError(f"Unknown model specified: {self.my_model}")
        
    def blue_button_callback(self, channel):
        """Logic for handling a blue button press."""
        self.my_human_preference = 'b'
        self.my_last_interaction_time = datetime.datetime.now().timestamp()
        print("Blue button pressed!")
        self.button_pressed = True
        if self.my_model == "claude":
            self.my_state = State.SELECT_B_TXT
        elif self.my_model == "sdxl":
            self.my_state = State.SELECT_B_IMG
        else:
            raise ValueError(f"Unknown model specified: {self.my_model}")
            
    def call_claude2(self):
        # Construct the body dictionary
        body_dict = {
            "prompt": self.my_prompt,
            "max_tokens_to_sample": 150,
            "temperature": 1,
            "top_k": 250,
            "top_p": 0.999,
            "stop_sequences": ["\n\nHuman:"],
            "anthropic_version": "bedrock-2023-05-31"
        }

        # Serialize the body dictionary to a JSON string
        body_str = json.dumps(body_dict)

        # Construct the kwargs dictionary
        kwargs = {
            "modelId": "anthropic.claude-v2",
            "contentType": "application/json",
            "accept": "*/*",
            "body": body_str
        }

        # Invoke the model
        response = self.bedrock_runtime.invoke_model_with_response_stream(**kwargs)
        stream = response.get('body')
        if stream:
            for event in stream:
                chunk = event.get('chunk')
                if chunk:
                    completion = json.loads(chunk.get('bytes')).get('completion')
                    self.my_result_a += completion

        print(f"Completion A: {self.my_result_a}")
        self.my_state = State.INFERENCE_TXT_B

        # Invoke the model
        response = self.bedrock_runtime.invoke_model_with_response_stream(**kwargs)
        stream = response.get('body')
        if stream:
            for event in stream:
                chunk = event.get('chunk')
                if chunk:
                    completion = json.loads(chunk.get('bytes')).get('completion')
                    self.my_result_b += completion

        print(f"Completion B: {self.my_result_b}")

    def invoke_sdxl(self):
        """Specific logic for making a sdxl prediction."""
        body_dict = {
            "text_prompts": [{"text": self.my_prompt}],
            "cfg_scale": 10,
            "seed": randint(0, 1000),
            "steps": 50
        }
        
        # Serialize the dictionary to a JSON string
        body_str = json.dumps(body_dict)

        # Construct the outer dictionary
        kwargs = {
            "modelId": "stability.stable-diffusion-xl-v0",
            "contentType": "application/json",
            "accept": "application/json",
            "body": body_str
        }

        # Invoke the model
        response = self.bedrock_runtime.invoke_model(**kwargs)
        response_body = json.loads(response.get("body").read())

        results = response_body.get("artifacts")[0].get("base64")
        return results

    def handle_image_gen(self):
        image = self.invoke_sdxl()
        self.my_result_a = image
        self.my_state = State.INFERENCE_IMG_B
        image = self.invoke_sdxl()
        self.my_result_b = image        
        
    def handle_generation(self):
        """General method to make a prediction based on the model type."""        
        if self.my_model == "claude":
            self.my_state = State.INFERENCE_TXT_A
            self.call_claude2()
            self.my_state = State.REVIEW_TXT
        elif self.my_model == "sdxl":
            self.my_state = State.INFERENCE_IMG_A
            self.handle_image_gen()
            self.my_state = State.REVIEW_IMG
        else:
            raise ValueError(f"Unknown model specified: {self.my_model}")
        
        self.my_task.cancel()

    def save_results(self):
        """Write to disk so human preferences can be uploaded to S3 later."""
        if self.my_human_preference:
            data = {
                'timestamp': str(datetime.datetime.now()),
                'instruction': self.my_prompt,
                'model': self.my_model,
                'prompt': self.my_prompt,
                'result_a': self.my_result_a,
                'result_b': self.my_result_b,
                'human_preference': self.my_human_preference,
            }

            try:
                with open(f"/results/{self.my_uuid}.json", "w") as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                print(f"An error occurred while saving results: {e}")
        self.get_next_prompt()
    