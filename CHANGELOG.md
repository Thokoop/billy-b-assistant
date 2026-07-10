# Changelog

All notable changes to this project will be documented in this file.

---

## [2.3.0] — 2026-07-10

### Added
- **openWakeWord Wake-word Support**: Added `openwakeword` as a local wake-word backend with ONNX wake-word model selection from `wakewords/`.
- **Grok Realtime Support**: Added `xAI` as a Realtime API provider option with support for Grok voice models in Billy's runtime and Web UI settings.
- **Grok Model Selection**: Added an `xAI Model` dropdown so Grok voice-capable models can be selected directly from the Web UI.
- **Unified Wi-Fi Settings**: Added Wi-Fi country, network scan, manual SSID entry, and save flow inside the main Billy Web UI.
- **Unified Captive Onboarding**: Added NetworkManager-managed open hotspot captive-portal-style for onboarding while preserving the legacy `wifi_setup.py` flow for existing builds through .env variable based mode selection. `WIFI_ONBOARDING_MODE=unified` Default: `WIFI_ONBOARDING_MODE=legacy`
- **Dedicated Wi-Fi Setup Modal**: Added a dedicated Wi-Fi setup modal for the unified onboarding flow, separate from the main settings save flow.
- **Status LED Support**: Added optional WS2812B status LED support with configurable enablement, backend, pin/count, brightness, DMA/PWM channel settings, and a Web UI test action.
- **Camera Rotation Setting**: Added `CAMERA_ROTATION` and Web UI rotation controls so camera preview and vision captures can be rotated in 90-degree steps.
- **Mic Test Calibration UI**: Added a live mic test readout with current RMS, rolling 10-second average RMS, and peak RMS values, plus matching visual markers inside the microphone level bar to make threshold tuning easier.
- **Mic Test Recording Playback**: Added a gated mic test recording flow so users can record and play back a short sample while testing microphone quality and speech-threshold behavior.
- **Startup Mic Pre-buffer**: Added a short microphone pre-buffer during session startup so speech captured after the wake-up sound but before the realtime connection is fully ready can still be sent once the session initializes.

### Changed
- **Generic Wake-word Uploads**: Wake-word upload flow now accepts both Porcupine `.ppn` keywords and openWakeWord `.onnx` models, stores them in `wakewords/`, and switches the selected backend automatically.
- **Wake-word Provider Settings UI**: Wake-word settings now show provider-specific controls for Porcupine and openWakeWord, including ONNX model selection and threshold configuration for openWakeWord.
- **Realtime Provider Settings**: Added a provider selector with provider-specific API key and model fields, while keeping `OpenAI` as the default unless explicitly changed.
- **Internet Settings Layout**: Moved device hostname and Web UI port into Internet Settings and renamed General Settings to API Settings.
- **Main Navigation Styling**: Moved feature navigation to footer. Simplified header navigation.
- **Mic Gain Persistence**: Added `MIC_GAIN` with a default of `max`, applies configured capture gain on startup, and saves Web UI mic gain changes to `.env`.

### Fixed
- **Session Stop Shortcut**: A double-press on the device button during an active session now ends the session immediately.
- **Mic RMS Consistency**: Shared the same local RMS calculation between the Web UI mic test and actual session mic handling, so the displayed calibration values match the threshold logic used during conversations.

---
---

## [2.2.3] — 2026-05-10

### Added
- **gpt-realtime-2 Support**: Added `gpt-realtime-2` to the OpenAI Realtime API Model dropdown in General Settings. The model is listed as the newest flagship option and is included in the conversation-state-enabled model set.

### Fixed
- **Audio Pitch Glitches**: Replaced FFT-based `resample()` with polyphase `resample_poly()` in the 24kHz→48kHz playback upsampling path. FFT resampling processes each audio chunk independently without phase continuity, causing audible pitch glitches at chunk boundaries during streaming playback. Polyphase filtering handles the exact 2× upsample ratio with smooth boundaries between chunks.

---

## [2.2.2] — 2026-04-21

### Fixed
 **Update Flow Stability (Web UI)**: Prevented `ModuleNotFoundError: app.sock_compat` from breaking Web UI startup paths that block update/restart actions.

## [2.2.1] — 2026-04-19

### Changed
- **Follow-up Reliability**: Improved follow-up turn handling for realtime sessions by treating valid audio-only user turns as meaningful even when provider user transcripts are unavailable.
- **Mic Timeout Behavior**: Made mic-timeout handling more robust for short/soft speech by using a more sensitive local activity pause threshold and better timeout state resets between listen windows.

### Fixed
- **Noise/Static Misclassification**: Reduced false “static/noise” classification when server VAD detects speech but local threshold crossings are low, using peak RMS and soft-speech checks.
- **Retry Budget Accounting**: Fixed follow-up retry-limit logic so meaningful audio turns reset/avoid consuming retry budget correctly instead of being counted as empty turns.
- **Session Teardown Hang Risk**: Hardened mic shutdown paths (`abort`/timeout guard) to prevent occasional hangs after `mic_timeout` stop.
- **Timeout Diagnostics**: Added clearer timeout diagnostics (start/progress/pause/end) to make mic-timeout behavior easier to trace in logs.
- **Debug Log**: Fixed debug log in the UX to append instead of overwrite and updated auto-scroll behavior so logs only auto-scroll when the `scroll-bottom` toggle is active.

---

## [2.2.0] — 2026-03-29

### Added
- **Porcupine Wake-word Support**: Added local wake-word activation using Picovoice Porcupine, including support for bundled and custom `.ppn` keyword files in `wakewords/`.
- **Wake-word UI Management**: Added Wake-word settings card with backend selection, access key input (with visibility toggle), keyword dropdown, keyword upload flow, and sensitivity control.
- **Wake-word Provider Architecture**: Introduced a pluggable wake-word provider structure (`wakeword_provider` + providers registry) to make adding future backends (for example openWakeWord) straightforward.
- **Camera Vision Tool**: Added a new `describe_scene` function-call tool so Billy can capture a fresh image from a Raspberry Pi camera (via `libcamera-still`) and describe what he sees by sending captured frames as `input_image` conversation content.
- **Camera Hardware Selection (Web UI)**: Added a `Camera Hardware` dropdown in Hardware Settings to choose between Raspberry Pi camera modules and USB webcams.
- **Camera Preview Test (Web UI)**: Added a `Test Camera` button that captures and shows a small inline preview image for the selected camera option.
- **Follow-up Retry Limit Setting**: Added `FOLLOW_UP_RETRY_LIMIT` to Web UI and config save flow with validated range `0..5` (default `1`).
- **Service Log Clear Action (Web UI)**: Added a `Clear log` action in the debug log toolbar to clear the current service logs from the UI.
- **Audio Device UX**: Added mic/speaker dropdown settings supporting stable `usbpath:` while maintaining backward compatibility with legacy name-based `.env` values. 

### Changed
- **Settings Layout**: Moved Pin Layout and Dory/Mode control into Advanced Settings to reduce clutter for default users.
- **Tool Instructions**: Updated model tool guidance to explicitly use camera vision when users ask Billy to look or describe the scene.
- **Hardware Settings Layout**: Moved microphone and speaker device dropdowns into Hardware Settings (above camera controls) for one-place hardware configuration.

---

## [2.1.1] — 2026-03-14

### Added
- **Wake-up Preview MQTT Bridge**: Added `billy/wakeup/play` MQTT topic handling so wake-up clip previews can be played by the running `billy.service` process (the GPIO owner), enabling mouth movement during preview from the Web UI.
- **Billy Berserk Persona Preset**: Added a new `billy-berserk` persona preset with an aggressive chaotic style.

### Changed
- **Wake-up Preview Routing**: Updated Web UI wake-up preview flow to prefer MQTT playback via `billy.service`, with `aplay` fallback in webconfig. This avoids importing core audio/movement modules in webconfig and prevents GPIO ownership conflicts.
- **User/Profile Switching (Web UI Backend)**: Updated `/current-user` routes to use and persist `CURRENT_USER` in `.env`, and to align `persona_manager` with the selected profile's `preferred_persona`.
- **Session Shutdown Resilience**: Improved stuck-session cleanup paths (button + MQTT stop/start flows) with stronger timeout handling and forced close fallbacks to reduce stale session thread lockups.

### Fixed
- **Persona Voice Persistence**: Fixed multiple realtime session update paths that changed instructions but not audio voice, causing voice to remain at `ballad` despite persona changes.
- **Persona Switch Consistency**: Fixed profile/persona switch path where voice-change handling could return early before fully applying the new persona state.
- **Session Startup Voice Selection**: Added safer persona voice resolution and startup logging to verify which persona voice is sent at connect time.
- **Web UI Profile Loading**: Fixed cases where switching users from the UI would still load fallback/default profile behavior instead of the selected user's stored persona context.

---

## [2.1.0] — 2026-03-12

### Added
- **OpenAI Realtime Model Support**: Added support for selecting `gpt-realtime-1.5` in the Web UI and `.env` configuration.
- **News Digest Tool**: Added a new `get_news_digest` function-call tool so Billy can fetch live headlines, weather forecasts, and sports scoreboards with optional topic, team, and regional filters.
- **News Sources Manager (Web UI)**: Added a UI list to add/remove/toggle RSS headline sources and persist them for Billy's news briefings.
- **MQTT commands**: Added discovery/config entities for `Billy Restart`, `Billy Reboot`, and `Billy Listen` on `billy/command`. Added support for the `listen` command over MQTT to start/stop Billy listening remotely. (contribution by: @Marko181)


### Changed

- **Restart Controls (Web UI)**: Removed the separate **Restart UI** button to avoid confusion. The remaining **Restart** button now performs the same full restart behavior as the old Restart UI action, restarting both `billy.service` and `billy-webconfig.service`.
- **WebSocket Integration**: Replaced HTTP polling with WebSocket for real-time status and log updates in the Web UI, significantly reducing network overhead and improving responsiveness.
- **Listening State Feedback**: Updated head movement timing so the head moves out when Billy is actually listening, rather than during the wake-up clip.
- **Moved .env editor**: Moved the .env editor from the header to the Advanced Settings section.
- **MQTT Startup Behavior**: MQTT broker connection now retries in the background until successful instead of failing once at startup. (contribution by: @Marko181)

### Fixed

- **Service Status Endpoint**: Fixed `/service/status` returning HTTP 500 when `billy.service` is inactive (for example during mic/speaker tests). The endpoint now reports inactive/failed states correctly instead of crashing.
- **Manual Interrupt Flow**: Simplified interruption handling back to button-first behavior. During a session, one press now interrupts the current response and reopens the mic, while a second press stops the session.
- **Session Audio Stability**: Improved wake-up-to-listening handoff so Billy more reliably waits for the wake-up sound to finish before opening the mic, reducing stuck startup states and false early listening behavior.
- **Listening and Shutdown Reliability**: Refined mic/session cleanup and state transitions to reduce hanging stops, duplicate stop handling, and noisy shutdown behavior during follow-up and retry flows.
- **User Greeting**: Fixed asyncio.sleep calls in user identification flow that prevented Billy from acknowledging new user profiles.
- **Wake-up sound**: Trimmed silent audio segments from the default wake-up sounds to improve responsiveness.

## [2.0.6] — 2026-03-05

### Fixed

- **Version Detection**: Fixed bug where pre-release/RC tags would still show up even if `SHOW_RC_VERSIONS=false`

---

## [2.0.5] — 2026-02-07

### Added

- **MQTT Song Endpoint**: Added MQTT command support to play songs via the `billy/song` topic.
- **Factory Reset Options**: Added option in the settings to reset configuration. (.env file, user profiles .ini's, custom personas .ini's, logs, git, wifi, reboot)

### Changed

- **Persona Form**: Renamed the identity section and combined voice controls into the “Persona Voice & Expression” area.

### Fixed

- **Provider Startup**: Missing API keys no longer hard-fail startup; a warning is logged and services continue.
- **Voice Options Load**: Config endpoint now handles missing realtime providers without crashing.

---

## [2.0.4] — 2025-02-02

### Added

- **Realtime AI Providers**: Introduced a `RealtimeAIProvider` abstraction with a provider registry to support multiple realtime AI backends (contribution by: @turekaj)
- **XAI Provider (Beta)**: XAI support is currently beta and not fully integrated into the UI yet. For now, enable it by adding `REALTIME_AI_PROVIDER=xai` and `XAI_API_KEY=XAI API KEY HERE` to your `.env` file manually. (contribution by: @turekaj)
- **Startup Flap Toggle**: Added `FLAP_ON_BOOT` setting and UI control to enable the startup flap animation only when desired. (contribution by: @turekaj)

### Fixed

- **Installation Documentation**: Added missing `swig` dependency to installation instructions. The `lgpio` Python package requires `swig` to build from source, which was causing installation failures.

---

## [2.0.3] — 2025-01-09

### Added

- **Mockfish Development Mode**: Added `MOCKFISH` environment variable to enable development and testing without physical hardware. When enabled, GPIO operations and button presses are mocked, allowing full testing of Billy's functionality on any machine without a Raspberry Pi or physical Billy Bass. (contribution by: @turekaj)

### Fixed

- **Version Detection**: Fixed issue where the updater would show an incorrect version (e.g., v2.0.1) even when a newer tag (e.g., v2.0.2) was checked out. Improved version detection logic now properly handles detached HEAD state and uses multiple fallback methods (`git tag --points-at HEAD`, `git describe --tags --exact-match`, `git describe --tags`) to accurately detect the current checked-out version.
- **Logging Integration**: Version detection and update processes now use the centralized logging system, respecting the `LOG_LEVEL` setting for appropriate verbosity.

---

## [2.0.2] — 2025-12-25

### Fixed

- **Song Interruption**: Fixed critical bug where interrupting a song (especially after multiple times) would cause the session to hang indefinitely. Button presses after interruption would have no effect. The fix adds proper interrupt checking in the song playback loop and uses a timeout-based approach instead of blocking indefinitely.
- **Documentation**: Added missing `liblgpio-dev` and `liblgpio1` system dependencies to installation instructions. These packages are required for the `lgpio` Python package to build successfully.

---

## [2.0.1] — 2025-12-07

### Fixed

- **MQTT Session Hanging**: Fixed issue where MQTT announcements without follow-up (e.g., "The washing machine is finished") would leave the session open indefinitely until hitting OpenAI's 60-minute timeout limit. Sessions now properly close when no follow-up is needed.

---

## [2.0.0] — 2025-11-02
>### 🎉 Major New Features:
> User Profiles with memories - Multiple Personas - Custom Song Manager - Mqtt Say command support for follow-ups 

>### ⚠️ For existing builds of Billy
>
> **If you're coming from <= v1.4.0:** Please select the Legacy Pin Layout in the Hardware Settings tab of the Web UI if you can't switch to the new unified wiring layout (see [BUILDME.md](./docs/BUILDME.md#from-motor-driver-to-raspberry-pi-gpio-pinout))

### Added

- **User Profiles**: Billy now identifies users when they introduce themselves (e.g., "Hi Billy, it's Thom")
  - Create/switch profiles, set display names, guest mode
  - Import/export profiles
  - Profile-specific statistics like interaction count and last seen timestamps
- **Memory System**: Billy remembers facts, preferences, and relationships for each user
  - Categorize by type (fact, preference, relationship, event, other)
  - Set importance levels (low, medium, high)
  - Edit/delete memories through UI
- **Multiple Personas**: Create and switch between different Billy personalities
  - Voices and mouth articulation per persona
  - Configurable personality traits (humor, sarcasm, honesty, etc.)
  - Import/export personas
  - Mid-conversation persona switching with graceful voice changes
  - Persona presets/templates for quick setup
- **Custom Song Manager**: Web UI for managing custom songs
  - Upload custom audio files (full.wav, vocals.wav, drums.wav)
  - Configure playback & animation settings (gain, tail threshold, compensate tail, head moves, half tempo tail flap)
  - Set song title and keywords for AI-triggered playback
  - Preview audio files before saving
  - Copy example songs to get started
- **UI Improvements**: 
  - 4-level configurable logging (ERROR/WARNING/INFO/VERBOSE)
  - Loading states and optimized polling

### Changed

- **Persona Storage**: Personas now stored in `./personas/persona_name/persona.ini`. The default persona remains at `./persona.ini` for backward compatibility.
- **Custom Wake-up Sounds**: Wake-up sounds are now saved per persona instead of globally
- **Logging System**: Replaced `DEBUG_MODE` with configurable `LOG_LEVEL` system (ERROR/WARNING/INFO/VERBOSE)
- **Mouth Articulation**: Now configured per persona instead of globally
- **Song Metadata Format**: Updated from `metadata.txt` to `metadata.ini` for consistency
- **Song Directory**: Custom songs now stored in `custom_songs/` (git-ignored) instead of `sounds/songs/` (which still holds the fishsticks song as a template)
- **MQTT Follow-up Handling**: MQTT prompts now respect `follow_up_intent` - if Billy asks a question via MQTT (e.g., "What's your favorite color?"), the mic stays open for your response in `auto` mode

### Fixed

- **Self-Triggering**: Billy no longer hears his own wake-up sounds
- **Head Movement**: Prevented head from getting stuck during routines
- **Session Hanging**: Added timeouts to prevent stuck sessions
- **Audio**: Standardized RMS calculations for consistent silence detection
- **Function Call Flow**: Fixed "conversation already has an active response" error by properly closing function calls with `function_call_output` before OpenAI auto-generates responses
- **GPIO Cleanup**: Fixed "double free or corruption" crash on shutdown by properly closing GPIO resources
- **Smart Home Commands**: Billy now correctly distinguishes between direct commands ("turn on lights") and questions ("ask if lights should be on") - no longer executes commands when asked to ask first
- **Function Confirmations**: Fixed issue where Billy wouldn't always provide spoken confirmation after smart home commands and personality changes - now reliably triggers verbal responses

---

## [1.5.0] — 2025-10-13

>### ⚠️ For existing builds of Billy: ⚠️ 
> 
> **Please select the Legacy Pin Layout in the Hardware Settings tab of the Web UI if you can't switch to the new unified wiring layout (see [BUILDME.md](./docs/BUILDME.md#from-motor-driver-to-raspberry-pi-gpio-pinout))**

### Added
- **Configurable Pin Layouts:** Introduced `BILLY_PINS` Pin Layout setting (`new` / `legacy`) to switch between the new (default) pin layout and the legacy pin layout (for builds before october '25)
- **Mouth Articulation Control:** Added `MOUTH_ARTICULATION` (1–10) setting to fine-tune speech motion responsiveness.
- **Error Sound Handling:**  Centralized error playback — now plays `error.wav`, `noapikey.wav`, or `nowifi.wav` depending on the issue.
- **Release notes notification:** Notification in the UI with the release notes of the latest version.

### Changed
- **Unified GPIO Logic:** Refactored motor control for both Modern (2-motor) and Classic (3-motor) models into a single system. Default pin assignments moved to safer GPIOs. Unused H-bridge inputs are now grounded;
- **Movement Refinements:** Improved PWM handling and non-blocking motion timing for smoother, more natural flapping.
- **UI Enhancements:** Added Billy artwork to header and included reboot/power/restart-ui buttons. Improved feedback for mic and speaker device tests.

### Fixed
- Minor mouth sync inconsistencies under load.
- Occasional stalls caused by blocking PWM threads.
- Better recovery after OpenAI API or network errors.
- Motor watchdog will now disengage any motor that is on > 30 seconds

---

## [1.4.0] — 2025-09-04

### Added

- **Say command**: Added a MQTT command to let billy announce messages, either as literal sentences or as prompts.
- **Custom Wake-up Sounds**: Custom Wake-up sounds can now be customised and generated via the UI
- **New gpt-realtime model**: Added Support for the new stable release of the openAI Realtime API model.
- **Favicon**: No more 404

### Changed

- Improved update process by re-installing python requirements on software update
- Updated personality traits prompt to be more descriptive and more distinct.
- Disabled Flask debug mode by default.

---

## [1.3.1] — 2025-08-19

### Added

- **Shutdown and restart**: Added raspberry pi shutdown and restart buttons in the UI (contribution by @cprasmu )

---

## [1.3.0] — 2025-07-28

### Added

- **'Dory' Mode**: Optional single-response mode where Billy answers only once before ending the session. (requested by @kenway33 )
- **Motor Test UI**: New motor test buttons in the Hardware tab allow triggering mouth and head/tail motion directly from the web interface. (requested by @henrym9)
- **Hostname + Port Configuration**: Added settings for customizing the device's hostname and Flask web port via UI. (requested by @cprasmu)
- **Import/Export**: Added ability to upload/download both `.env` and `persona.ini` files from the web UI.

### Changed

- Improved internal JS structure and modularization.
- Minor refinements to input label styling.
- Improved UX:
  - Added collapsible UI sections
  - **Reduced Motion Mode**: New UI toggle to disable animations and backdrop blur for accessibility or preference. Setting persists in local storage.
  - **Tooltips**: Informational tooltips added to multiple UI elements for better user guidance.

### Fixed

- **Motor Retract Fix**: Ensures Billy's head reliably returns to neutral after session ends.

---

## [1.2.0] — 2025-07-24

### Added

- Web-based user interface for easy configuration of Billy
  - Versioning check logic during boot & button to trigger OTA update
  - Speaker volume test and control in UI.
  - Tailwind CSS included locally for styling.
  - Password field visibility toggles in the UI.
  - Dropdown for selecting voice options in UI.
  - Mic input and speaker output level test utility.
- Option to change openAI Model
- Compatibilty for Classic Billy Model with 3 motors

### Changed

- Folder structure simplified and clarified.
- Automatic creation of `.env` and `persona.ini` from *.example files on first run.
- Committed `persona.ini`; now ignored by `.gitignore`.

### Added in beta

- MQTT "say" command integration for announcing messages
- Systemd service install process.
- Wi-Fi onboarding form (captive portal)
