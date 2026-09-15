"""PipeWire node discovery and framed PCM transport (no hardware imports)."""

import json
import subprocess


SOURCE_CLASSES = {"Audio/Source", "Stream/Output/Audio"}
SINK_CLASSES = {"Audio/Sink", "Stream/Input/Audio"}


def node_kind(name, media_class):
    """Normalise a bluez node to Audio/Source or Audio/Sink, or None.

    A2DP transports appear as Audio/Source. HFP transports appear as
    Stream/Output/Audio and Stream/Input/Audio instead, so matching only the
    Audio/* spellings silently drops every call. The node name already states
    the direction, so trust that and accept either spelling of the class.
    """
    if name.startswith("bluez_input.") and media_class in SOURCE_CLASSES:
        return "Audio/Source"
    if name.startswith("bluez_output.") and media_class in SINK_CLASSES:
        return "Audio/Sink"
    return None


def bluetooth_nodes(objects):
    devices = {
        str(item["id"]): item.get("info", {}).get("props", {})
        for item in objects
        if item.get("type") == "PipeWire:Interface:Device"
    }
    result = []
    for item in objects:
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        props = item.get("info", {}).get("props", {})
        name = props.get("node.name", "")
        kind = node_kind(name, props.get("media.class", ""))
        if kind is None:
            continue
        device = devices.get(str(props.get("device.id")), {})
        address = device.get("api.bluez5.address") or device.get("bluez5.address")
        # device.id is shared by both HFP directions, even if no address is exposed.
        identity = address or str(props.get("device.id", ""))
        if identity:
            result.append({
                "name": name,
                "kind": kind,
                "device": identity,
                "state": item.get("info", {}).get("state"),
            })
    return result


def discover():
    result = subprocess.run(
        ["pw-dump"], capture_output=True, text=True, check=True, timeout=5
    )
    return bluetooth_nodes(json.loads(result.stdout))


def select_route(nodes):
    """Fail closed for multiple phones; never send the mic to another device."""
    devices = {node["device"] for node in nodes}
    if len(devices) > 1:
        raise RuntimeError("Disconnect other phones before starting the audio test")
    sources = [node for node in nodes if node["kind"] == "Audio/Source"]
    sinks = [node for node in nodes if node["kind"] == "Audio/Sink"]
    if len(sources) > 1 or len(sinks) > 1:
        raise RuntimeError("Ambiguous Bluetooth audio route")
    if not sources:
        return None
    return sources[0]["name"], sinks[0]["name"] if sinks else None


def command(target, *, record, latency_ms=20):
    return [
        "pw-cat",
        "--record" if record else "--playback",
        "--rate=48000",
        "--channels=1",
        "--format=s16",
        f"--latency={latency_ms}ms",
        f"--target={target}",
        "--properties={\"node.dont-reconnect\":true}",
        "-",
    ]


class PCMFrames:
    """Preserve partial pipe reads; expose exact 20 ms mono s16 frames."""

    def __init__(self, frame_bytes=1920):
        self.pending = bytearray()
        self.frame_bytes = frame_bytes

    def feed(self, chunk):
        self.pending.extend(chunk)
        frames = []
        while len(self.pending) >= self.frame_bytes:
            frames.append(bytes(self.pending[: self.frame_bytes]))
            del self.pending[: self.frame_bytes]
        return frames
