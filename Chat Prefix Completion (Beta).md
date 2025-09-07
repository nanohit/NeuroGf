Chat Prefix Completion (Beta)

The chat prefix completion follows the Chat Completion API, where users provide an assistant's prefix message for the model to complete the rest of the message.

Notice

When using chat prefix completion, users must ensure that the role of the last message in the messages list is assistant and set the prefix parameter of the last message to True.
The user needs to set base_url="https://api.deepseek.com/beta" to enable the Beta feature.
Sample Code

Below is a complete Python code example for chat prefix completion. In this example, we set the prefix message of the assistant to "```python\n" to force the model to output Python code, and set the stop parameter to ['```'] to prevent additional explanations from the model.

from openai import OpenAI

client = OpenAI(
    api_key="<your api key>",
    base_url="https://api.deepseek.com/beta",
)

messages = [
    {"role": "user", "content": "Please write quick sort code"},
    {"role": "assistant", "content": "```python\n", "prefix": True}
]
response = client.chat.completions.create(
    model="deepseek-chat",
    messages=messages,
    stop=["```"],
)
print(response.choices[0].message.content)

