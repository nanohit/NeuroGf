Gemini API libraries
When building with the Gemini API, we recommend using the Google GenAI SDK. These are the official, production-ready libraries that we develop and maintain for the most popular languages. They are in General Availability and used in all our official documentation and examples.

Note: If you're using one of our legacy libraries, we strongly recommend you migrate to the Google GenAI SDK. Review the legacy libraries section for more information.
If you're new to the Gemini API, follow our quickstart guide to get started.

Language support and installation

The Google GenAI SDK is available for the Python, JavaScript/TypeScript, Go and Java languages. You can install each language's library using package managers, or visit their GitHub repos for further engagement:

Python
JavaScript
Go
Java
Library: google-genai
GitHub Repository: googleapis/python-genai
Installation: pip install google-genai
General availability

We started rolling out Google GenAI SDK, a new set of libraries to access Gemini API, in late 2024 when we launched Gemini 2.0.

As of May 2025, they reached General Availability (GA) across all supported platforms and are the recommended libraries to access the Gemini API. They are stable, fully supported for production use, and are actively maintained. They provide access to the latest features, and offer the best performance working with Gemini.

If you're using one of our legacy libraries, we strongly recommend you migrate so that you can access the latest features and get the best performance working with Gemini. Review the legacy libraries section for more information.

Legacy libraries and migration

If you are using one of our legacy libraries, we recommend that you migrate to the new libraries.

The legacy libraries don't provide access to recent features (such as Live API and Veo) and are on a deprecation path. They will stop receiving updates on November 30th, 2025, the feature gaps will grow and potential bugs may no longer get fixed.

Each legacy library's support status varies, detailed in the following table:

Language	Legacy library	Support status	Recommended library
Python	google-generativeai	All support, including bug fixes, ends on November 30th, 2025.	google-genai
JavaScript/TypeScript	@google/generativeai	All support, including bug fixes, ends on November 30th, 2025.	@google/genai
Go	google.golang.org/generative-ai	All support, including bug fixes, ends on November 30th, 2025.	google.golang.org/genai
Dart and Flutter	google_generative_ai	Not actively maintained	Use trusted community or third party libraries, like firebase_ai, or access using REST API
Swift	generative-ai-swift	Not actively maintained	Use Firebase AI Logic
Android	generative-ai-android	Not actively maintained	Use Firebase AI Logic
Note for Java developers: There was no legacy Google-provided Java SDK for the Gemini API, so no migration from a previous Google library is required. You can start directly with the new library in the Language support and installation section.