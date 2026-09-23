#!/usr/bin/python3
# Route cellular call audio to a connected Bluetooth HFP headset: CS-Voice to
# the internal BT SCO ports while the headset is connected, and the headset in
# HFP with its SCO link up while a call is active.

import json
import os
import signal
import subprocess
import sys

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import Gio, GLib, GLibUnix

CARD = "msm8916"
BT_RX = "INT_BT_SCO_RX"
BT_TX = "INT_BT_SCO_TX"
RATE_CTL = "Internal BT SCO SampleRate"
HFP_PROFILES = ("headset-head-unit-cvsd", "headset-head-unit")
MM_ACTIVE = {1, 2, 4}  # DIALING, RINGING_OUT, ACTIVE
STATE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "msm8916-bt-call.ports")


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, check=False)


def amixer_get(name):
    out = run("amixer", "-c", CARD, "cget", f"name={name}").stdout
    for line in out.splitlines():
        if ": values=" in line:
            return line.split(": values=", 1)[1].strip()
    return None


def amixer_set(name, value):
    if run("amixer", "-c", CARD, "-q", "cset", f"name={name}", value).returncode:
        log(f"cannot set '{name}' to {value}")


def rx_ctl(port):
    return f"{port} Voice Mixer CS-Voice"


def tx_ctl(port):
    return f"CS-Voice Capture Mixer {port}"


def voice_ports():
    """Return the (rx, tx) backend ports CS-Voice is routed to."""
    rx = tx = None
    out = run("amixer", "-c", CARD, "controls").stdout
    for line in out.splitlines():
        name = line.split("name=", 1)[-1].strip("'")
        if name.endswith(" Voice Mixer CS-Voice") and amixer_get(name) == "on":
            rx = name[: -len(" Voice Mixer CS-Voice")]
        elif name.startswith("CS-Voice Capture Mixer ") and amixer_get(name) == "on":
            tx = name[len("CS-Voice Capture Mixer "):]
    return rx, tx


def pactl_json(*what):
    out = run("pactl", "-f", "json", "list", *what).stdout
    try:
        return json.loads(out)
    except ValueError:
        return []


def find_headset():
    """Return (card, profiles) for a connected HFP-capable headset."""
    for card in pactl_json("cards"):
        name = card.get("name", "")
        profiles = card.get("profiles") or {}
        # availability flips while a profile switch is in flight
        if name.startswith("bluez_card.") and any(p in profiles for p in HFP_PROFILES):
            return name, profiles
    return None, {}


def hfp_profile(profiles):
    for prof in HFP_PROFILES:
        if profiles.get(prof, {}).get("available"):
            return prof
    return next((p for p in HFP_PROFILES if p in profiles), None)


def a2dp_profile(profiles):
    a2dp = [(v.get("priority", 0), k) for k, v in profiles.items()
            if k.startswith("a2dp-") and v.get("available")]
    return max(a2dp)[1] if a2dp else None


def hfp_source(card):
    """Return (node name, codec) of the headset's own HFP source node."""
    addr = card.split(".", 1)[1].replace("_", ":")
    try:
        objs = json.loads(run("pw-dump").stdout)
    except ValueError:
        return None, None
    for obj in objs:
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("api.bluez5.address") == addr and \
                props.get("factory.name") == "api.bluez5.sco.source":
            return props.get("node.name"), props.get("api.bluez5.codec")
    return None, None


def active_profile(card):
    for c in pactl_json("cards"):
        if c.get("name") == card:
            return c.get("active_profile")
    return None


class Policy:
    def __init__(self):
        self.headset = None
        self.saved_ports = None
        self.calls = set()
        self.in_call = False
        self.holder = None
        self.restore_profile = None

    def update_headset(self):
        card, _ = find_headset()
        if card == self.headset:
            return
        self.headset = card
        if card:
            log(f"headset {card}: voice routes to Bluetooth")
            self.route_to_bt()
        else:
            log("headset gone: voice routes back")
            self.release_link()
            self.route_back()

    def route_to_bt(self):
        rx, tx = voice_ports()
        if rx != BT_RX or tx != BT_TX:
            self.saved_ports = (rx, tx)
            try:
                with open(STATE, "w") as f:
                    json.dump(self.saved_ports, f)
            except OSError:
                pass
        elif not self.saved_ports:
            # a previous instance moved the routes; take over its record
            try:
                with open(STATE) as f:
                    self.saved_ports = tuple(json.load(f))
            except (OSError, ValueError):
                log("routes already on Bluetooth and no record of the previous ones")
        amixer_set(RATE_CTL, "8000")
        amixer_set(rx_ctl(BT_RX), "on")
        amixer_set(tx_ctl(BT_TX), "on")

    def route_back(self):
        if not self.saved_ports:
            return
        rx, tx = self.saved_ports
        if rx:
            amixer_set(rx_ctl(rx), "on")
        if tx:
            amixer_set(tx_ctl(tx), "on")
        self.saved_ports = None
        try:
            os.unlink(STATE)
        except OSError:
            pass

    def hold_link(self):
        card, profiles = find_headset()
        prof = hfp_profile(profiles)
        if not card or not prof or self.holder:
            return
        # back to A2DP afterwards, whatever is active now
        self.restore_profile = a2dp_profile(profiles) or active_profile(card)
        run("pactl", "set-card-profile", card, prof)
        src = codec = None
        for _ in range(30):
            src, codec = hfp_source(card)
            if src:
                break
            GLib.usleep(100000)
        if not src:
            log(f"no HFP source after switching {card} to {prof}")
            return
        # A running stream on the bluez node is what keeps the SCO link up
        self.holder = subprocess.Popen(
            ["pw-record", f"--target={src}", "/dev/null"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.route_to_bt()
        log(f"call on {card}: {prof}, codec {codec}, holding {src}")
        if codec != "cvsd":
            # q6voiced configures the SCO port as the call starts
            log(f"codec {codec} does not match the 8 kHz SCO port")

    def release_link(self):
        if self.holder:
            self.holder.terminate()
            try:
                self.holder.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.holder.kill()
            self.holder = None
        if self.headset and self.restore_profile:
            run("pactl", "set-card-profile", self.headset, self.restore_profile)
        self.restore_profile = None

    def call_state(self, path, old, new):
        if new in MM_ACTIVE:
            self.calls.add(path)
        else:
            self.calls.discard(path)
        in_call = bool(self.calls)
        if in_call == self.in_call:
            return
        self.in_call = in_call
        if in_call and self.headset:
            self.hold_link()
        elif not in_call:
            self.release_link()


def main():
    policy = Policy()

    def on_mm_signal(conn, sender, path, iface, signal, params):
        old, new, _reason = params.unpack()
        policy.call_state(path, old, new)

    system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    system.signal_subscribe(None, "org.freedesktop.ModemManager1.Call",
                            "StateChanged", None, None,
                            Gio.DBusSignalFlags.NONE, on_mm_signal)

    sub = None

    def on_pactl(stream, cond):
        line = stream.readline()
        if not line:
            # pipewire-pulse went away; follow it when it comes back
            sub.wait()
            GLib.timeout_add_seconds(2, subscribe)
            return False
        if " on card " in line:
            policy.update_headset()
        return True

    def subscribe():
        nonlocal sub
        sub = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
        GLib.io_add_watch(sub.stdout, GLib.PRIORITY_DEFAULT,
                          GLib.IOCondition.IN | GLib.IOCondition.HUP, on_pactl)
        policy.update_headset()
        return False

    subscribe()
    loop = GLib.MainLoop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, sig, loop.quit)
    try:
        loop.run()
    finally:
        policy.release_link()
        policy.route_back()
        if sub:
            sub.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
