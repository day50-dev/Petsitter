SCENARIOS = [
    {
        "name": "json_mode_simple",
        "description": "Return a simple JSON object with name, age, city",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Return a JSON object with keys: name, age, city. "
                    "Use name 'Alice', age 30, city 'NYC'. "
                    "Respond with ONLY valid JSON, no other text."
                ),
            }
        ],
        "params": {},
        "trick_paths": ["tricks/json_mode.py"],
        "scorer": "json_valid",
    },
    {
        "name": "json_mode_nested",
        "description": "Return nested JSON with user and address",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Return JSON with a 'user' object containing 'name', "
                    "'address' (with 'street', 'city'), and 'phone'. "
                    "Respond with ONLY valid JSON, no other text."
                ),
            }
        ],
        "params": {},
        "trick_paths": ["tricks/json_mode.py"],
        "scorer": "json_valid",
    },
    {
        "name": "json_required_keys",
        "description": "Return JSON with specific required fields",
        "messages": [
            {
                "role": "user",
                "content": (
                    "Return a JSON object describing a book with "
                    "title, author, year, and genre. "
                    "Respond with ONLY valid JSON, no other text."
                ),
            }
        ],
        "params": {},
        "trick_paths": ["tricks/json_mode.py"],
        "scorer": "has_required_keys",
        "scorer_args": {"keys": ["title", "author", "year"]},
    },
    {
        "name": "tool_call_weather",
        "description": "Request weather data via tool call",
        "messages": [
            {"role": "user", "content": "What's the weather like in New York City?"}
        ],
        "params": {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get current weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "city": {
                                    "type": "string",
                                    "description": "City name",
                                },
                                "units": {
                                    "type": "string",
                                    "enum": ["celsius", "fahrenheit"],
                                },
                            },
                            "required": ["city"],
                        },
                    },
                }
            ],
        },
        "trick_paths": ["tricks/tool_call.py"],
        "scorer": "tool_call_format",
    },
    {
        "name": "tool_call_multi_tool",
        "description": "Pick correct tool from multiple options",
        "messages": [
            {
                "role": "user",
                "content": "Send an email to alice@example.com saying hello.",
            }
        ],
        "params": {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "send_email",
                        "description": "Send an email",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "to": {"type": "string"},
                                "body": {"type": "string"},
                            },
                            "required": ["to", "body"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather for a city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                },
            ],
        },
        "trick_paths": ["tricks/tool_call.py"],
        "scorer": "tool_call_format",
    },
    {
        "name": "combined_json_and_tool",
        "description": "Tool call with JSON arguments",
        "messages": [
            {
                "role": "user",
                "content": "Create a new user profile with name 'Bob', age 25, and save it.",
            }
        ],
        "params": {
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "create_profile",
                        "description": "Create a user profile",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "age": {"type": "integer"},
                            },
                            "required": ["name", "age"],
                        },
                    },
                }
            ],
        },
        "trick_paths": ["tricks/json_mode.py", "tricks/tool_call.py"],
        "scorer": "tool_call_format",
    },
]
