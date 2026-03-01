Petsitter is an openai compatible proxy that adds some functionality to models that they can handle with a little bit of guidance.

For example: Many smaller models may not do toolcalling for instance. However, toolcalling can be hacked.

In Petsitting we call these tricks.

Every trick is a subclass of the Trick class

```python
from petsitter import callmodel

class Trick: 

    def system_prompt(to_add: Str) -> Str:
        """
        Adds an instruction to the system prompt. This is similar to how many tools
        have .rules and works in the same way
        """
        # For instance, say we want to do toolcalling:
        return "IMPORTANT: To call tools Respond only with the json '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"<insert function name>\",\"arguments\":{<insert paramaters here>}}}'. You will get the response in the reply."
        return ""

    def pre_hook(context: List, params: Dict) -> List:
        """
        This is the context before it hits the model. You can
        run whatever you feel is appropriate on it and then return
        the context to go to the model
        """

        # The params are other parameters (such as skills, toolcalls or
        # other functions that went into the HTTP POST)
        #
        # For instance, here we're going to grab the tools that were passed
        if 'tools' in params:
            # This means we only have the system-prompt and the first message
            if len(context) == 2: 
                context[0]['content'] += f"The tools you have access to are {json.dumps(params['tools'])}"

        return context

    def post_hook(context: List) -> List:
        """
        This is called after the model processes its response but
        before it goes up stream
        """

        # Example 1: structured output
        # Pretend you are looking for pure json output and want
        # to do a call loop back to the model until it gets it right
        # This is what callmodel is for. Let's do an example
        pre_hook_length = len(context)
        attempts = 10

        while attempts > 0:
            try:
                json.loads(context[-1])
                break
            except:
                context = callmodel(context, "Hrmm, we need your response to be parsable valid json, not conversational, and not using any markdown. Let's try that again")
                attempts -= 1

        return context[:pre_hook_length] + [context[-1]]

        # Example 2: the toolcall hack ... 
        # discover that the model is doing our hacky toolcall and then do something like
        context[-1]['tool_call'] = [
			{
				"id": "call_abc123",
				"type": "function",
				"function": {
				  "name": "get_order_status",
				  "arguments": "{\"order_id\": \"12345\"}"
				}
      		}
		]
		delete context[-1]['content']

		return context



    def info(capabilities: Dict) -> Dict:
        """
        If you are adding a feature, such as tool-calling, you'll
        need to mutate the capabilities dictionary. This is for 
        harnesses that check if a tool has a capability before
        proceeding
        """
        return capabilities
```

Here is what it might look like in practice:

```bash
$ ollama serve

$ petsitter --model_url http://localhost:11434 --model_name somemodel:8b --trick tricks/tools.py --trick tricks/json.py --listen_on localhost:8080
```

And then you point your agentic coding program to `localhost:8080`.


