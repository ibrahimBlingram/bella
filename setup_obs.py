"""
setup_obs.py — one-shot: build the OBS scene + sources Bello expects.

Run AFTER OBS is open with the WebSocket server enabled (port/password from
config.yaml). Safe to re-run: it skips anything that already exists.

    .venv/bin/python setup_obs.py
"""
from pathlib import Path
import yaml
import obsws_python as obs

ROOT = Path(__file__).resolve().parent
cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
o = cfg["obs"]

c = obs.ReqClient(host=o["host"], port=o["port"], password=o["password"])
scene = o["scene"]

# 1. Scene
existing_scenes = [s["sceneName"] for s in c.get_scene_list().scenes]
if scene not in existing_scenes:
    c.create_scene(scene)
    print(f"created scene: {scene}")
else:
    print(f"scene exists: {scene}")

# 2. Media sources (idle avatar, talk avatar, background)
media = [
    (o["avatar_idle_source"], o["avatar_idle_loops"][0], True),   # visible at start
    (o["avatar_talk_source"], o["avatar_talk_loops"][0], False),  # hidden at start
    (o["background_source"],  o["demo_backgrounds"][0], True),
]

existing_inputs = [i["inputName"] for i in c.get_input_list().inputs]
for name, first_file, visible in media:
    if name in existing_inputs:
        print(f"source exists: {name}")
    else:
        c.create_input(
            scene, name, "ffmpeg_source",
            {"local_file": first_file, "looping": True, "is_local_file": True},
            True,
        )
        print(f"created source: {name}  ({first_file})")
    # ensure correct start visibility
    item_id = c.get_scene_item_id(scene, name).scene_item_id
    c.set_scene_item_enabled(scene, item_id, visible)

# 3. Make "Live" the active program scene
c.set_current_program_scene(scene)
print(f"\n[OK] OBS scene '{scene}' is ready with AvatarIdle / AvatarTalk / Background.")
