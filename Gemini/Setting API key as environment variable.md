Setting API key as environment variable

If you set the environment variable GEMINI_API_KEY or GOOGLE_API_KEY, the API key will automatically be picked up by the client when using one of the Gemini API libraries. It's recommended that you set only one of those variables, but if both are set, GOOGLE_API_KEY takes precedence.

If you're using the REST API, or JavaScript on the browser, you will need to provide the API key explicitly.

Here is how you can set your API key locally as the environment variable GEMINI_API_KEY with different operating systems.

Linux/macOS - Bash
macOS - Zsh
Windows
Bash is a common Linux and macOS terminal configuration. You can check if you have a configuration file for it by running the following command:


~/.bashrc
If the response is "No such file or directory", you will need to create this file and open it by running the following commands, or use zsh:


touch ~/.bashrc
open ~/.bashrc
Next, you need to set your API key by adding the following export command:


export GEMINI_API_KEY=<YOUR_API_KEY_HERE>
After saving the file, apply the changes by running:


source ~/.bashrc
Providing API key explicitly

In some cases, you may want to explicitly provide an API key. For example:

You're doing a simple API call and prefer hard coding the API key.
You want explicit control without having to rely on automatic discovery of environment variables by the Gemini API libraries
You're using an environment where environment variables are not supported (e.g web) or you are making REST calls.
Below are examples for how you can provide an API key explicitly:

Python
JavaScript
Go
Java
REST

from google import genai

client = genai.Client(api_key="YOUR_API_KEY")

response = client.models.generate_content(
    model="gemini-2.5-flash", contents="Explain how AI works in a few words"
)
print(response.text)
Keep your API key secure

Treat your Gemini API key like a password. If compromised, others can use your project's quota, incur charges (if billing is enabled), and access your private data, such as files.

Critical security rules

Never commit API keys to source control. Do not check your API key into version control systems like Git.
Never expose API keys on the client-side. Do not use your API key directly in web or mobile apps in production. Keys in client-side code (including our JavaScript/TypeScript libraries and REST calls) can be extracted.
Best practices

Use server-side calls with API keys The most secure way to use your API key is to call the Gemini API from a server-side application where the key can be kept confidential.
Use ephemeral tokens for client-side access (Live API only): For direct client-side access to the Live API, you can use ephemeral tokens. They come with lower security risks and can be suitable for production use. Review ephemeral tokens guide for more information.
Consider adding restrictions to your key: You can limit a key's permissions by adding API key restrictions. This minimizes the potential damage if the key is ever leaked.
For some general best practices, you can also review this support article.