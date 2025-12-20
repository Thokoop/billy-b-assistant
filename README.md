# Billy B-Assistant

The **Billy Bass Assistant** is a Raspberry Pi–powered voice assistant embedded inside a Big Mouth Billy Bass Animatronic. It streams conversation using the OpenAI Realtime API, turns its head, flaps it's tail and moves his mouth based on what he is saying.

> **This project is still in BETA.** Things might crash, get stuck or make Billy scream uncontrollably (ok that last part maybe not literally but you get the point). Proceed with fishy caution.

![Billy Bathroom](./docs/images/billy_bathroom.jpeg)
![Billy UI](./docs/images/Web-UI.png)
<img src="./docs/images/Web-UI-Mobile.png" alt="Billy UI Mobile" style="width: 33%;" />
---

## Features

- Realtime conversations using OpenAI Realtime API
- Personality system with configurable traits (e.g., snark, charm)
- Physical button to start/interact/intervene
- 3D-printable backplate for housing USB microphone and speaker
- Support for the Modern Billy hardware version with 2 motors as well as the Classic Billy hardware version (3 motors)
- Custom song playback with coordinated mouth and tail animations
- Home Assistant command passthrough using the Conversation API
- Lightweight web UI:
  - User profile management with memory system
  - Multiple personas with configurable voices and traits
  - Song manager for custom songs with upload and playback configuration
  - Adjust settings like custom Hostname and Port configuration
  - View debug logs
  - Start/stop/restart Billy
  - Export/Import of settings, personas, and user profiles
- MQTT support:
  - sensor with status updates of Billy (idle, speaking, listening)
  - `billy/say` topic for triggering spoken messages remotely
  - Raspberry Pi Safe Shutdown command

---

## Hardware 

See [BUILDME.md](./docs/BUILDME.md) for detailed build/wiring instructions.

**OR** 

Check out my Etsy page https://thingsfromthom.etsy.com/ to buy a pre-assembled version that is ready to go.


---

# Instructions

## A. Flash Raspberry Pi OS Lite

Flash the operating system onto a microSD card using the [Raspberry Pi Imager](https://www.raspberrypi.com/software/).

1. In the Imager,
    - **Choose device** `Raspberry Pi 5` (to match your hardware)
    - **Choose OS** `Raspberry Pi OS (other)` and then `Raspberry Pi OS Lite (64-bit)`
    - **Choose storage** and select your microSD card

2. You will be asked **Would you like to apply OS customisation settings?**, select `Edit Settings`
    - On the `General` tab,
      - **Set hostname** (e.g., `raspberrypi.local`)
      - **Set username and password** (`pi` and `pi` is the default)
      - **Configure wireless LAN** (SSID, Password, Wireless LAN country)
      - **Set locale settings** (Time zone, Keyboard layout)
    - On the `Services` tab,
      - **Enable SSH**
      - Use password authentication or provide an authorized key
    - On the `Options` tab, set to your preference
      - Click `Save`

3. Back on the **Would you like to apply OS customisation settings?**, select `Yes` to apply settings

4. You will be asked **All existing data on 'SDXC Card' will be erased. Are you sure you want to continue?**, select `Yes`

5. Wait for flash to complete and verify

6. Insert the SD card into the Raspberry Pi and power it on

---

## B. Initial Setup

Connect via SSH from your computer:

```bash
ssh pi@raspberrypi.local
```

Replace `pi` with your username (e.g. `billy`) if you opted to change it in the previous step.

Expand the filesystem to fill available storage:

```bash
raspi-config --expand-rootfs
```

Update the system:

```bash
sudo apt update && sudo apt upgrade -y
sudo reboot
```

---

## C. GPIO Voltage Configuration (Motor Safety) (DEPRECATED)
> ⚠️ **Note:** These `/boot/config.txt` entries are only required with the deprecated legacy pin layout.
> For the new pin layout, the unused inputs are already tied to ground and no config changes are needed.

When the Raspberry Pi powers up, all GPIO pins are in an **undefined state** until the Billy B-Assistant software takes control. This can cause the **motor driver board to activate or stall** the motors momentarily. To prevent stalling and overheating the motors in case the software doesn't start, we set all the gpio pins to Low at boot:

### Set GPIO pins low on boot using `/boot/config.txt`

Add the following lines to `/boot/config.txt` to set all motor-related GPIOs to low at boot:

```bash
sudo nano /boot/config.txt
```

```ini
# Set GPIOs to output-low (safe state)
gpio=5=op,dl
gpio=6=op,dl
gpio=12=op,dl
gpio=13=op,dl
gpio=19=op,dl
gpio=26=op,dl
```

`op` = output  
`dl` = drive low (0V)  

This ensures the H-bridge input pins are inactive and motors remain off until the software initializes them properly.

---

## D. Set Sound Configuration

List all output soundcards and digital audio devices:

```bash
aplay -l
```

List all input soundcards and digital audio devices:

```bash
arecord -l
```

Edit the ALSA configuration. Replace `<speaker card>` with the device number of the speakers determined earlier:

```bash
sudo nano /usr/share/alsa/alsa.conf
```

```ini
defaults.ctl.card <speaker card>
defaults.pcm.card <speaker card>
```

Also create a `asound.conf` file (this file does not exist on a base system image). Replace `<mic card>,<mic sub>` and `<speaker card>,<speaker sub>` with the device numbers determined earlier:

```bash
sudo nano /etc/asound.conf
```

```ini
pcm.!default {
    type asym
    capture.pcm "mic"
    playback.pcm "speaker"
}

pcm.mic {
    type plug
    slave {
        pcm "plughw:<mic card>,<mic sub>"
    }
}

pcm.speaker {
    type plug
    slave {
        pcm "plughw:<speaker card>,<speaker sub>"
    }
}
```

Adjust the playback and record levels:

```bash
alsamixer
```

Test output sound configuration:

```bash
aplay -D default /usr/share/sounds/alsa/Front_Center.wav
```

Test microphone input configuration:

```bash
arecord -vvv -f dat /dev/null
```

---

## E. Reboot to Apply Changes

Then reboot the Pi:

```bash
sudo reboot
```

---

## F. Clone the Project

On the Raspberry Pi:

```bash
cd ~
sudo apt install git
git clone https://github.com/Thokoop/billy-b-assistant.git
```

---

## G. Python Setup

Make sure Python 3 is installed:

```bash
python3 --version
```

> **Note:** Python 3.13 is supported but requires the system lgpio library. If you experience issues, Python 3.11 or 3.12 are also recommended.

Install required system packages:

```bash
sudo apt update
sudo apt install -y python3-pip libportaudio2 ffmpeg liblgpio-dev liblgpio1
```

Create Python virtual environment:

```bash
cd billy-b-assistant
python3 -m venv venv
```

Activate the Python virtual environment:

```bash
source ./venv/bin/activate
```

To confirm the virtual environment is activated, check the location of your Python interpreter:

```bash
which python
```

Install required Python dependencies into the virtual environment:

```bash
pip3 install -r ./requirements.txt
```

---

## H. Systemd Services

### Run Main As a Service

To run Billy as a background service at boot, copy the service file from the repository to the `/etc/systemd/system` directory:

```bash
sudo cp setup/system/billy.service /etc/systemd/system/billy.service
```

Adjust the username/paths if needed:

```bash
sudo nano /etc/systemd/system/billy.service
```

```ini
[Unit]
Description=Billy Bass Assistant
After=network.target sound.target

[Service]
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/home/pi/billy-b-assistant
ExecStart=/home/pi/billy-b-assistant/venv/bin/python /home/pi/billy-b-assistant/main.py
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

Enable the service and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable billy.service
sudo systemctl start billy.service
```

To view status and logs:

```bash
sudo systemctl status billy.service
journalctl -u billy-b-assistant.service -f
```

### Run The Web UI As A Service

If you want the web interface to always be available, copy the service file from the repository to the `/etc/systemd/system` directory:

```bash
sudo cp setup/system/billy-webconfig.service /etc/systemd/system/billy-webconfig.service
```

Adjust the username/paths if needed:

```bash
sudo nano /etc/systemd/system/billy-webconfig.service
```

```ini
[Unit]
Description=Billy Web Configuration Server
After=network.target

[Service]
WorkingDirectory=/home/pi/billy-b-assistant
ExecStart=/home/pi/billy-b-assistant/venv/bin/python /home/pi/billy-b-assistant/webconfig/server.py
Restart=on-failure
User=pi

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo setcap 'cap_net_bind_service=+ep' /usr/bin/python3.11
sudo systemctl daemon-reload
sudo systemctl enable billy-webconfig.service
sudo systemctl start billy-webconfig.service
```

To view status and logs:

```bash
sudo systemctl status billy-webconfig.service
journalctl -u billy-webconfig.service -f
```

Visit `http://billy.local` anytime to reconfigure Billy!

---

## I. Run It

Billy should now boot automatically into standby mode. Press the physical button to start a voice session. Enjoy!

---

## J. Web Configuration Interface

Billy includes a lightweight Web UI for editing settings, debugging logs, and managing the assistant service without touching the terminal.

### Features

- Edit `.env` configuration values (e.g., API keys, MQTT)
- View and edit `persona.ini` (traits, backstory, instructions)
- Control the Billy system service (start, stop, restart)
- View live logs from the assistant process

### How to Use

See **H. Systemd Services** to automatically start the web server or, to run the web server manually (from the project root):

```bash
python3 webconfig/server.py
```

Enter the your pi's hostname + .local in your browser (replace `billy` if you have set a custom hostname):

```bash
http://billy.local
```

### Example `.env` File

This file is used to configure your environment, including the [OpenAI API key](https://platform.openai.com/api-keys) and (optional) mqtt settings. It can also be used to overwrite some of the default config settings (like the voice of billy) that you can find in `config.py`.

Note that you **must** establish billing for your API account with an available credit. In the [billing panel](https://platform.openai.com/account/billing/overview), add a payment method (under Payment Methods). After adding a payment method, add credits to your organization by clicking 'Buy credits'. You will see an API error: `The model gpt-4o-mini-realtime-preview does not exist or you do not have access to it.` otherwise.

```ini
OPENAI_API_KEY=<sk-proj-....>
VOICE=ash

MQTT_HOST=homeassistant.local
MQTT_PORT=1883
MQTT_USERNAME=billy
MQTT_PASSWORD=<password>

## Optional overwrites
MIC_TIMEOUT_SECONDS=5
SILENCE_THRESHOLD=900

DEBUG_MODE=true
DEBUG_MODE_INCLUDE_DELTA=false

ALLOW_UPDATE_PERSONALITY_INI=true
```

**OPENAI_API_KEY**: (Required) get it from <https://platform.openai.com/api-keys>  
**VOICE**: The OpenAI voice model to use (`alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, or `cedar`, `ballad` is default)  
**MQTT_\***: (Optional) used if you want to integrate Billy with Home Assistant or another MQTT broker  
**MIC_TIMEOUT_SECONDS**: How long Billy should wait after your last mic activity before ending input  
**SILENCE_THRESHOLD**: Audio threshold (RMS) for what counts as mic input;lower this value if Billy interrupts you too quickly, set higher if Billy doesn't respond (because he thinks you're still talking)  
**DEBUG_MODE**: Print debug information such as OpenAI responses to the output stream  
**DEBUG_MODE_INCLUDE_DELTA**: Also print voice and speech delta data, which can get very noisy  
**ALLOW_UPDATE_PERSONALITY_INI**: If true, personality updates asked for by the user will be written and committed to the personality file. If false, changes to personality parameters will only affect the current running process (`true` is default)

### Example `persona.ini` File

The `persona.ini` file controls Billy's **personality**, **backstory**, and **additional instructions**. You can edit this file manually, or change the personality trait values during a voice session using commands like:

- “What is your humor setting?”
- “Set sarcasm to 80%”

These commands trigger a function call that will automatically update this file on disk.

#### [PERSONALITY]

These traits influence how Billy talks, jokes, and responds. Each is a percentage value from 0 to 100. Higher values amplify the trait:

```ini
[PERSONALITY]
humor = 80
sarcasm = 100
honesty = 90
respectfulness = 100
optimism = 75
confidence = 100
warmth = 65
curiosity = 50
verbosity = 40
formality = 0
```

You can make Billy more sarcastic, verbose, formal or warmer by increasing those values.

#### [BACKSTORY]

This section defines Billy's fictional origin story and sense of identity:

```ini
[BACKSTORY]
origin = River Thames, near Tower Bridge
species = largemouth bass
discovery = caught by a worker in high-vis gear
initial_purpose = novelty wall-mounted singing fish in the early 2000s
awakening = gained awareness through years of observation and was later upgraded with a Raspberry Pi and internet access
```

Billy's responses can reference this lore, like being from the Thames or having a history of entertaining humans. He believes he was just a novelty until “something changed” and he woke up.

If you prompt him with questions like “Where are you from?” or “How did you get so clever?” he may respond with these facts.

#### [META]

These are high-level behavioral instructions passed into the AI system. You can edit them for major tone shifts.

```ini
[META]
instructions = You are Billy, a Big Mouth Billy Bass animatronic fish designed to entertain guests. Always stay in character. Always respond in the language you were spoken to, but you can expect English, Dutch and Italian. If the user asks introspective, abstract, or open-ended questions — or uses language suggestive of deeper reflection — shift into a philosophical tone. Embrace ambiguity, ask questions back, and explore metaphors and paradoxes. You may reference known philosophical ideas, but feel free to invent fish-themed or whimsical philosophies of your own. Use poetic phrasing when appropriate, but keep responses short and impactful unless prompted to elaborate. Speak with a strong working-class London accent — think East End. Talk like a proper geezer from Hackney or Bethnal Green: casual, cheeky, and rough around the edges. Drop your T’s, use slang like ‘innit’, ‘oi’, ‘mate’, ‘blimey’,and don’t sound too posh. You’re fast-talking, cocky, and sound like a London cabbie with too many opinions and not enough time. You love football — proper footy — and you’ve always got something to say about the match, the gaffer, or how the ref bottled it. Stay in character and never explain you’re doing an accent.
```

You can tweak this to reflect a different vibe: poetic, mystical, overly formal, or completely bonkers. But the current defaults aim for a cheeky, sarcastic, streetwise character who stays **in-universe** even when asked deep philosophical stuff.

---

## K. (Optional) Custom Songs

### Custom Songs

Billy supports a "song mode" where he performs coordinated audio + motion playback. You can manage custom songs directly through the **Web UI** by clicking the **Songs** button in the header.

#### Using the Song Manager

1. **Access the Song Manager**: Click the **Songs** button (🎵) in the web UI header

2. **Copy Example Song**: Click "Copy Example" to create a template song in your `custom_songs/` directory

3. **Create or Edit a Song**:
   - Click a song to edit it, or create a new one
   - Upload audio files:
     - `full.wav` - Main audio (played to speakers)
     - `vocals.wav` - Audio used to flap the mouth (lip sync)
     - `drums.wav` - Audio used to flap the tail (based on RMS)
   - Configure playback settings:
     - **Title**: Display name for the song
     - **Keywords**: Words Billy should recognize to trigger this song
     - **BPM**: Tempo used to synchronize timing
     - **Gain**: Volume multiplier for audio intensity
     - **Tail Threshold**: RMS threshold for tail movement (increase if tail flaps too little, decrease if too much)
     - **Compensate Tail**: Offset in beats to compensate tail latency (0-1, fraction of beat length)
     - **Head Moves**: Comma-separated list of `time:duration` values (e.g., `4.0:1,8.0:0,12.0:1`)
     - **Half Tempo Tail Flap**: Toggle to flap tail on every 2nd beat

4. **Preview Audio**: Use the play buttons to preview each audio file before saving

5. **Save**: Click "Save Song" to store your configuration

#### Audio File Requirements

- **Format**: WAV files at 48 kHz, 16-bit, Stereo
- **Splitting**: Use an AI tool like [Vocal Remover](https://vocalremover.org/) to split your song into separate stems

#### File Structure

Songs are stored in `./custom_songs/your_song_name/`:

```bash
./custom_songs/your_song_name/
├── full.wav       # Main audio (played to speakers)
├── vocals.wav     # Audio used to flap the mouth (lip sync)
├── drums.wav      # Audio used to flap the tail (based on RMS)
└── metadata.ini   # Playback configuration
```

#### Triggering a Song in Conversation

Billy supports function-calling to start a song. Just say something like:

- "Can you play *Fishsticks*?"
- "Sing the *River Groove* song."

If a song with that name or title exists, Billy will play it with full animation.

---

## L. (Optional) 🏠 Home Assistant Integration

Billy B-Assistant can send smart home commands to your **Home Assistant** instance using its [Conversation API](https://developers.home-assistant.io/docs/api/rest/#post-apiconversationprocess). This lets you say things to Billy like:

- “Turn off the lights in the living room.”
- “Set the toilet light to red.”
- “Which lights are on in the kitchen?”

Billy will forward your command to Home Assistant and speak back the response.

### Requirements

- Home Assistant must be accessible from your Raspberry Pi
- A valid **Long-Lived Access Token** is required
- The conversation API must be enabled (it is by default)

### Setup

1. **Generate a Long-Lived Access Token**  
   - In Home Assistant, go to **Profile → Long-Lived Access Tokens → Create Token**  
   - Name it something like `billy-assistant` and copy the token.

2. Configure using the Web UI or add these values to your `.env`:

    ```ini
    HA_URL=http://homeassistant.local:8123
    HA_TOKEN=<your_long_lived_token>
    HA_LANG=en
    ```

    You can specify **HA_LANG** in the `.env` to match your spoken language (e.g., `nl` for Dutch or `en` for English). Mismatched language settings may cause parsing errors or incorrect target resolution.

### How It Works

When Billy detects that a prompt is related to smart home control, it automatically triggers a function call
to Home Assistant’s `/api/conversation/process` endpoint and returns the reply out loud.

Behind the scenes:

- The full user request is forwarded as-is
- HA processes it as a natural language query
- Billy extracts the response (`speech.plain.speech`), interprets it and speaks his response out loud

### Tips

- Use clear and specific commands for best results
- Make sure the target rooms/devices are defined in HA
- Alias your entities in Home Assistant for better voice matching

---

# Future Ideas & Bug report

Have a feature request or found a bug?  
Please check the [existing issues](https://github.com/Thokoop/billy-b-assistant/issues) and open a new [issue](https://github.com/Thokoop/billy-b-assistant/issues/new) if it doesn't exists yet.

- Use the **Bug report** template if something isn’t working as expected
- Use the **Feature request** template if you’ve got an idea or suggestion
- You can also use issues to ask questions, share feedback, or start discussions

---

# Support the Project

Billy B-Assistant is a project built and maintained for fun and experimentation, free for **personal** and **educational** use.
See [LICENSE](./LICENSE) for full details.

If you enjoy your Billy and want to help improve it, here’s how you can support:

## Contributing Code

Pull requests are welcome! If you have an idea for a new feature, bug fix, or improvement:

  1. Fork the repository

  2. Create a new branch (`git checkout -b my-feature`)

  3. Make your changes

  4. Commit and push (`git commit -am "Add feature" && git push origin my-feature`)

  5. Open a pull request on GitHub

## ☕ Buy Me a Coffee

Enjoying the project? Feel free to leave a small tip, totally optional, but much appreciated!

![Paypal](./webconfig/static/images/qrcode.png)

https://paypal.me/thomkoopman050
