# Billy B-Assistant – Build Instructions

This guide explains how to physically build and wire your Raspberry Pi–powered **Billy Bass Assistant**.

---

## Parts List

- Modern Billy version:
  - [3D-printed custom backplate](https://makerworld.com/en/models/1457024-ai-fish-billy-big-mouth-bass-backplate#profileId-1518677)
- Classic (& Christmas edition) version: 
  - [Alternative 3D-printed parts](https://www.thingiverse.com/thing:7096350)

| Part                                         | Source                                                                                                                                                     |
|----------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------|
| Big Mouth Billy Bass                         | [Amazon NL](https://amzn.eu/d/gzyNRsg)                                                                                                                     |
| MicroSD card (for Raspberry OS Lite)         | [Kiwi Electronics](https://www.kiwi-electronics.com/nl/transcend-64gb-microsd-met-adapter-uhs-i-u3-a2-ultra-performance-160-80-mb-s-11632 )                |
| Raspberry Pi 5 (4gb RAM or more recommended) | [Kiwi Electronics](https://www.kiwi-electronics.com/nl/raspberry-pi-5-computers-accessoires-415/raspberry-pi-5-4gb-11579)                                  |
| Raspberry Pi Power Supply 27W                | [Kiwi Electronics](https://www.kiwi-electronics.com/nl/raspberry-pi-27w-usb-c-power-supply-wit-eu-11581)                                                   |
| MicroSD card (for Raspberry OS Lite)         | [Kiwi Electronics](https://www.kiwi-electronics.com/nl/transcend-64gb-microsd-met-adapter-uhs-i-u3-a2-ultra-performance-160-80-mb-s-11632 )                |
| Raspberry Pi Active Cooler                   | [Kiwi Electronics](https://www.kiwi-electronics.com/nl/raspberry-pi-active-cooler-11585)                                                                   |
| 1x USB Speaker                               | [Amazon NL](https://amzn.eu/d/2yklfno), [Amazon US](https://www.amazon.com/dp/B075M7FHM1), [AliExpress](https://aliexpress.com/item/1005007168026736.html) |
| 1x USB Microphone                            | [Amazon NL](https://amzn.eu/d/7Y9GhoL), [Amazon US](https://www.amazon.com/dp/B08M37224H), [AliExpress](https://aliexpress.com/item/1005007211513791.html) |
| 1x L298N Motor Driver                        | [Amazon NL](https://amzn.eu/d/g9yBNVg), [Amazon US](https://www.amazon.com/dp/B0B82GZVT5), [AliExpress](https://aliexpress.com/item/1005006890733953.html) |
| 1x WS2812B LED (optional status light)       | [Amazon NL](https://www.amazon.nl/dp/B0DRJMGT9C) |                                                                                                          |
| Jumper Wires / Dupont Cables                 | [Amazon NL](https://amzn.eu/d/i4kyXG2), [AliExpress](https://aliexpress.com/item/1005003641187997.html)                                                    |
| JST 2.54 4 pin connector (female) \*         | [Amazon NL](https://amzn.eu/d/cDqHgNv), [AliExpress](https://aliexpress.com/item/1005007460897865.html)                                                    |


\* Recommended to be able to easily (un)plug the motor cables.

## Tools List

- Soldering iron
- Flush cutter
- Pliers
- Screwdriver (small Philips)
- Wire stripper (recommended)
- Drill bit (~2mm diameter)
- Glue (optional)
- Double sided mounting tape or appropriate screws(for mounting french cleat)

---

## A. Gut the Fish

![Original Wiring](./images/original_wiring.jpeg)

> Note that different editions of the Billy might have different hardware arrangements and wiring colors.

- Remove the 6 philips screws in the back of the Billy and remove the original backplate. Save the screws for re-assembly later
- Unplug all the electronic plugs, only keep the two motors + the JST plug (with Red Black White Orange wires) and the push button. Remove the piezoelectric speaker (disc shaped with blue wires) and the light/motion sensor (yellow wires)
- In the spot where the sensor was, the microphone will be placed so cut away the plastic extrusion until it is (somewhat) flush to the rest of the housing (I used the flush cutter to cut away chunks of plastic and used the pliers to twist & turn to remove the plastic)

## B. Wiring Instructions

### From Motor to Motor Driver

Solder either (90 degree corner) header pins on INT1-INT4 and + - to be able to use intact female to female jumper wires.
or cut the jumper wires keeping the female plug intact and solder the bare wire ends onto the board.

Solder the other end of the JST plug onto the board. The order is important.
In my case the colors of the plug don't match the colors of the original motor cables (see photo below),
therefore included the JST wire color column is included.

If you don't use the JST connector and solder the motor wires directly
to the driver board or if you have a JST connector with different wire colors / order,
focus only on the motor wire color and the corresponding pin on the driver board.

The table below is considered with motor driver board oriented with the Motor B and Motor A labels in the
bottom left corner of the board. The 1-4 positions are counted from left to right.

| Motor Driver Pin (position) | Wire Color JST | Motor Wire Color |
|-----------------------------|----------------|------------------|
| Motor B (1)                 | Black          | Red              |
| Motor B (2)                 | White          | White            |
| Motor A (3)                 | Yellow         | Orange           |
| Motor A (4)                 | Red            | Black            |

![Motor to Driver](./images/motor_driver.jpeg)
![JST connector](./images/jst_connector.jpeg)

### From Motor Driver to Raspberry Pi (GPIO Pinout)

Using distinct jumper wire colors is recommended for easier identification.

There are two selectable wiring profiles (`BILLY_PINS` in .env or via the web UI):
- `new` (default, quieter GPIO layout)
- `legacy` (old wiring)

---

#### New Profile (Default, quiet GPIO layout)
In this mode, each motor only uses one PWM input pin.
The unused IN pins on the driver(s) must be tied to their GND so they never float. 
This keeps the driver in a known safe state and prevents “phantom” motor movement at boot.

- Modern Billy (2 motors): Head and Tail share one driver, direction selects which moves.
- Classic Billy (3 motors): Each motor has its own single PWM pin; mates tied low.

**Modern Billy (2 motors)**

| Component | GPIO Pin (Physical)             | Motor Driver Input |
|-----------|---------------------------------|--------------------|
| Head      | GPIO 22 (pin 15)                | Pin IN1            |
| Tail      | GPIO 27 (pin 13)                | Pin IN2            |
| Mouth     | GPIO 17 (pin 11)                | Pin IN3            |
| Motor +   | 5v pwr  (pin 4)                 | Pin +              |
| Motor -   | Ground  (pin 6)                 | Pin -              |
| Button    | GPIO 24 (pin 18) & GND (pin 20) |                    |

Optional status LED:
- Default (`STATUS_LED_BACKEND=auto`):
  - Raspberry Pi 5: wire WS2812B `DIN` -> SPI MOSI GPIO 10 (pin 19)
  - Older Pi boards using PWM: wire WS2812B `DIN` -> GPIO 18 (pin 12)
- WS2812B `5V` -> 5v power (pin 2 or 4)
- WS2812B `GND` -> Ground (for example pin 6)
- Set `STATUS_LED_ENABLED=true` in `.env` after wiring it
- Optional: force a backend with `STATUS_LED_BACKEND=spi` or `STATUS_LED_BACKEND=pwm`
---

**Classic Billy (3 motors)**

| Component | GPIO Pin (Physical)             | Motor Driver Input |
|-----------|---------------------------------|--------------------|
| Head      | GPIO 22 (pin 15)                | Driver 1 Pin IN1   |
| Tail      | GPIO 27 (pin 13)                | Driver 2 Pin IN1   |
| Mouth     | GPIO 17 (pin 11)                | Driver 1 Pin IN3   |
| Motor +   | 5v pwr  (pin 4)                 | Driver 1 & 2 Pin + |
| Motor -   | Ground  (pin 6)                 | Driver 1 & 2 Pin - |
| Button    | GPIO 24 (pin 18) & GND (pin 20) |                    |


![GPIO pins](./images/gpio-pins.png)


#### Legacy Profile (for backwards compatibility)

| Component         | GPIO Pin (Physical)             | Motor Driver Input    |
|-------------------|---------------------------------|-----------------------|
| Head IN1 (PWM)    | GPIO 13 (pin 33)                | Controller 1 IN1      |
| Head IN2          | GPIO 6  (pin 31)                | Controller 1 IN2      |
| Mouth IN1 (PWM)   | GPIO 12 (pin 32)                | Controller 1 IN3      |
| Mouth IN2         | GPIO 5  (pin 29)                | Controller 1 IN4      |
| Tail IN1 (PWM) \* | GPIO 19 (pin 35)                | Controller 2 IN1      |
| Tail IN2 \*       | GPIO 26 (pin 37)                | Controller 2 IN2      |
| Motor +           | 5v pwr  (pin 4)                 | Power to motor driver |
| Motor -           | Ground  (pin 6)                 |                       |
| Button            | GPIO 27 (pin 13) & GND (pin 14) | Trigger voice session |

\* Only used on Classic Billy (3 motors).

![Assembly overview 1](./images/assembly_1.jpeg)
![Assembly overview 2](./images/assembly_2.jpeg)

---


### Speaker and mic

Using an existing USB speaker and USB microphone is the easiest route.

Remove the Philips screw in the back of the speaker to be able to separate the front and the back of the speaker housing.
Take note / a picture of the wiring of the USB cable at the audio board (probably from left to right: Black, Green, White and Red) and cut the cable at ~ 15cm to reduce the unneeded cable length.
Remove the wire from the back of the housing and resolder the 4 USB cables accordingly.
We only need one speaker but in the backplate there are two speaker spots:

- one facing to the side of the housing, a couple holes need to be drilled in the original housing to create a 'speaker grill' for sound to pass through. This spot is recommended since it will place the speaker closest to the mouth of Billy.
- one facing downwards (like the original placement). This is for future-proofing in case of a different method of mounting (standalone / not flush against the wall)

Place the speaker in the speaker holder and place it onto the backplate.

Remove the top part of the microphone housing and place the microphone component itself in the 3D printed holder and place it on the backplate.
These holders should be a snug fit. If not; use a bit of glue to secure it.

![Assembly overview 3](./images/assembly_3.jpeg)
In the picture above both speaker spots are used.
The 'downward' facing speaker is only recommended when there is a bit of space for the sound between the wall and billy.
The microphone is not shown in this photo since my initial idea was to glue it in place in the original housing.
Afterwards I added the mic holder for easier assembly.

## C. Assemble

> Before connecting the motors and powering up the Raspberry Pi it is recommended to have completed at least step **C. GPIO Voltage Configuration (Motor Safety)** of the [README.md](./../README.md)

Once all the components are placed into the backplate (and everything is connected) the backplate can be mounted onto the original housing.
Route the USB-C cable through the existing hole on the top and first attach only 2 of the 6 screws.
Continue with the software installation if you haven't done so already and do a first test run of Billy.
If everything works as expected screw in the remaining screws.

![Assembly overview 4](./images/assembly_4.jpeg)

The French cleat can be mounted on the wall with screws or double sided (mounting) tape. Billy can then be placed onto the cleat.
