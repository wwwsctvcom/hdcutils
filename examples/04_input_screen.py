# -*- coding: utf-8 -*-
"""04 - input, screen and power: screenshots, taps, swipes, keys.

Low-level input injection only (uitest uiInput, the OpenHarmony equivalent
of adb's `input` command); no UI automation layer is involved.

Run: python examples/04_input_screen.py
"""
import time

import hdcutils

hdc = hdcutils.HdcClient()
d = hdc.device()

# 1. Screenshot -> PIL.Image (or raw JPEG bytes without Pillow).
img = d.screenshot("screen.jpeg")
print("screenshot   :", type(img).__name__, "saved to screen.jpeg")

# 2. Screen size, parsed from the screenshot JPEG SOF marker (rotation aware).
size = d.window_size()
print("window_size  :", size, "-> %sx%s" % (size.width, size.height))

# 3. Touch input.
cx, cy = size.width // 2, size.height // 2
d.click(cx, cy)                    # uitest uiInput click x y
d.double_click(cx, cy)
d.long_click(cx, cy)
d.swipe(cx, int(size.height * 0.8), cx, int(size.height * 0.2))   # swipe up
d.drag(cx, cy, cx + 100, cy + 100)

# 4. Keys and text (official uitest uiInput subcommands).
d.key_event(hdcutils.KeyCode.BACK)          # keyEvent <keyCode>
d.key_event("Home")                         # or the documented name
d.text("hdcutils")                          # `text <content>` -> focused field
d.input_text(100, 200, "typed")             # `inputText <x> <y> <text>`
d.fling(cx, int(size.height * 0.8), cx, int(size.height * 0.2))   # fling
d.dirc_fling(3)                             # dircFling 3 = down
d.volume_up()
d.volume_down()

# 5. Official hdc command names are available as aliases, e.g.
#    d.target_boot("recovery")  ==  d.target_boot("recovery")  -> `hdc target boot recovery`
#    d.smode()                  ==  d.smode()             -> `hdc smode`
#    d.tmode_port(10123)        ==  d.tmode_port(10123)       -> `hdc tmode port 10123`

# 6. Power state.
print("screen on    :", d.is_screen_on())
d.screen_off()
time.sleep(0.5)
d.screen_on()
d.unlock()                                 # wake + swipe up (password-free lock)

# 7. Battery info (hidumper BatteryService, best-effort parse).
try:
    print("battery      :", d.battery())
except hdcutils.HdcCommandError as exc:
    print("battery unavailable:", exc)
