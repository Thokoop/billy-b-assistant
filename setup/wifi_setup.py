import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, redirect, render_template_string, request


app = Flask(__name__)

wifi_request = {}
ONBOARDING_FLAG = Path(__file__).resolve().parent / ".wifi_onboarding_active"

FORM_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Connect Billy to Wi-Fi</title>
  <style>
    body {
      background-color: #111;
      color: #eee;
      font-family: sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 1rem;
    }
    form {
      background: #222;
      padding: 2rem;
      border-radius: 10px;
      box-shadow: 0 0 10px #000;
      width: 100%;
      max-width: 400px;
    }
    label {
      display: block;
      margin-bottom: 1rem;
    }
    input, select, button {
      width: 100%;
      padding: 0.75rem;
      margin-top: 0.5rem;
      border: none;
      border-radius: 5px;
      font-size: 16px;
    }
    button {
      background-color: #4caf50;
      color: white;
      font-weight: bold;
    }
    p {
      margin-top: 1rem;
      text-align: center;
    }
  </style>
  <script>
    if (location.protocol === "https:") {
      location.href = "http://" + location.hostname + location.pathname;
    }
  </script>
</head>
<body>
<h2>Connect Billy to Wi-Fi</h2>
<form method="POST">
  <label>SSID:
    <input name="ssid" required />
  </label>
  <label>Password:
    <input name="password" type="password" required />
  </label>
  <label>Country:
    <select name="country" required>
      <option value="">-- Select your country --</option>
      <option value="US">🇺🇸 United States</option>
      <option value="GB">🇬🇧 United Kingdom</option>
      <option value="DE">🇩🇪 Germany</option>
      <option value="NL">🇳🇱 Netherlands</option>
      <option value="FR">🇫🇷 France</option>
      <option value="ES">🇪🇸 Spain</option>
      <option value="IT">🇮🇹 Italy</option>
      <option value="JP">🇯🇵 Japan</option>
      <option value="KR">🇰🇷 South Korea</option>
      <option value="IN">🇮🇳 India</option>
      <option value="CN">🇨🇳 China</option>
      <option value="BR">🇧🇷 Brazil</option>
      <option value="CA">🇨🇦 Canada</option>
      <option value="AU">🇦🇺 Australia</option>
    </select>
  </label>
  <button type="submit">Connect</button>
</form>
</body>
</html>
"""

CONNECTING_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Connecting...</title>
  <style>
    body {
      background-color: #111;
      color: #eee;
      font-family: sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 1rem;
      text-align: center;
    }
    p {
      font-size: 1.2rem;
      margin-top: 1rem;
    }
  </style>
  <script>
    setTimeout(() => {
      fetch("/", { method: "HEAD", cache: "no-store" }).catch(() => {
        document.body.innerHTML = `
          <h2>✅ Wi-Fi connected!</h2>
          <p>Billy is now online and the hotspot is turned off.</p>
          <p>You may now return to the main app at <code>billy.local</code>.</p>
        `;
      });
    }, 3000);
  </script>
</head>
<body>
  <h2>✅ Billy is connecting to <code>{{ ssid }}</code></h2>
  <p>You may lose connection shortly. Please reconnect to your Wi-Fi and visit <code>billy.local</code>.</p>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def onboarding():
    if request.method == "POST":
        ssid = request.form["ssid"]
        password = request.form["password"]
        country = request.form["country"]

        wifi_request["ssid"] = ssid
        wifi_request["password"] = password
        wifi_request["country"] = country

        return redirect("http://192.168.4.1:8080/connecting")

    return render_template_string(FORM_TEMPLATE)


@app.route("/connecting")
def connecting():
    ssid = wifi_request.get("ssid")
    if not ssid:
        return redirect("/")

    threading.Thread(target=handle_connection, daemon=True).start()
    return render_template_string(CONNECTING_TEMPLATE, ssid=ssid)


def handle_connection():
    ssid = wifi_request.get("ssid")
    password = wifi_request.get("password")
    country = wifi_request.get("country")

    # Let the "connecting" page reach the phone before the hotspot goes away.
    time.sleep(3)
    # hostapd owns wlan0 in AP mode; NetworkManager cannot join a network
    # through it, so the hotspot has to be gone before connecting.
    stop_hotspot_services()

    if save_wifi_credentials(ssid, password, country):
        print(f"✅ Connected to {ssid}")
    else:
        print(f"❌ Failed to connect to {ssid}; reopening Wi-Fi setup")
        # The startup check finds no connection and opens the hotspot again.
        subprocess.run(
            ["sudo", "systemctl", "restart", "--no-block", "billy-wifi-check.service"],
            check=False,
        )

    # Stopping this unit ends this process, so it must be the last step. It
    # used to run inside save_wifi_credentials, which killed the setup before
    # the hotspot was stopped and left Billy unreachable until a reboot.
    subprocess.run(["sudo", "systemctl", "stop", "billy-wifi-setup.service"])


def save_wifi_credentials(ssid, password, country):
    try:
        # Set Wi-Fi regulatory domain
        subprocess.run(["iw", "reg", "set", country], check=True)

        subprocess.run(["sudo", "systemctl", "start", "NetworkManager"], check=True)
        subprocess.run(
            ["sudo", "nmcli", "device", "set", "wlan0", "managed", "yes"], check=False
        )

        subprocess.run(["nmcli", "radio", "wifi", "off"], check=False)
        time.sleep(2)
        subprocess.run(["nmcli", "radio", "wifi", "on"], check=False)
        subprocess.run(["nmcli", "dev", "wifi", "rescan"], check=False)
        time.sleep(2)
        subprocess.run(["nmcli", "dev", "wifi", "list"], check=False)
        time.sleep(2)

        subprocess.run(
            [
                "sudo",
                "nmcli",
                "--wait",
                "60",
                "dev",
                "wifi",
                "connect",
                ssid,
                "password",
                password,
                "ifname",
                "wlan0",
                "name",
                ssid,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"❌ Connection failed: {e}")
        return False

    # Joined: some networks block ping, so this only informs the log.
    time.sleep(5)
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "3", "8.8.8.8"], stdout=subprocess.DEVNULL
    )
    if result.returncode != 0:
        print("⚠️ Connected, but 8.8.8.8 did not answer a ping")
    return True


def stop_hotspot_services():
    subprocess.call(["sudo", "systemctl", "stop", "hostapd"])
    subprocess.call(["sudo", "systemctl", "stop", "dnsmasq"])
    subprocess.call(["sudo", "rm", "-f", "/etc/dnsmasq.d/billy-captive-portal.conf"])
    # wifi_check.sh gave wlan0 the hotspot address by hand.
    subprocess.call(["sudo", "ip", "addr", "del", "192.168.4.1/24", "dev", "wlan0"])
    if ONBOARDING_FLAG.exists():
        ONBOARDING_FLAG.unlink()
    print("🛑 Stopped hotspot services")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
