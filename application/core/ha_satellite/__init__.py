"""Home Assistant Assist satellite over the ESPHome native API (experimental).

Billy presents itself to Home Assistant's ESPHome integration as a voice
satellite. Home Assistant runs the Assist pipeline (speech-to-text,
conversation agent and text-to-speech), so no OpenAI or xAI key is needed.

This package is a protocol spike: it speaks the plaintext ESPHome API only,
does not touch audio hardware or motors and is not yet registered as a voice
provider. Importing it has no side effects.
"""
