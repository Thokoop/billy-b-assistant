import configparser

from flask import Blueprint, jsonify, request, send_file

from ..state import PERSONA_PATH


bp = Blueprint("persona", __name__)

MOOD_PRESETS = {
    "neutral",
    "calm",
    "cheerful",
    "warm",
    "curious",
    "focused",
    "playful",
    "mischievous",
    "excited",
    "surprised",
    "sleepy",
    "bored",
    "sad",
    "anxious",
    "flustered",
    "annoyed",
    "grumpy",
    "dramatic",
}


def _normalize_wakeup_moods(value) -> str:
    if isinstance(value, str):
        raw_values = value.split(",")
    elif isinstance(value, list):
        raw_values = value
    else:
        raw_values = []
    moods = []
    for mood in raw_values:
        normalized = str(mood).strip().lower()
        if normalized in MOOD_PRESETS and normalized not in moods:
            moods.append(normalized)
    return ",".join(moods)


@bp.route("/persona", methods=["GET"])
def get_default_persona():
    config = configparser.ConfigParser()
    config.read(PERSONA_PATH)
    return jsonify({
        "PERSONALITY": dict(config["PERSONALITY"]) if "PERSONALITY" in config else {},
        "BACKSTORY": dict(config["BACKSTORY"]) if "BACKSTORY" in config else {},
        "META": config["META"].get("instructions", "") if "META" in config else "",
        "WAKEUP": dict(config["WAKEUP"]) if "WAKEUP" in config else {},
    })


@bp.route("/persona/current/mouth-articulation")
def get_current_persona_mouth_articulation():
    """Read-only: the mouth articulation level the active persona currently
    falls back to (same source core.movements._articulation_multiplier()
    reads). Used by the Song editor to show a sensible default - has no
    side effects, unlike GET /persona/<name> which switches the active persona.
    """
    from core.persona_manager import persona_manager

    level = 5
    try:
        data = persona_manager.get_current_persona_data() or {}
        raw = data.get('meta', {}).get('mouth_articulation')
        if raw not in (None, ''):
            level = max(0, min(10, float(raw)))
    except Exception:
        pass
    return jsonify({"mouth_articulation": level})


@bp.route("/persona/<persona_name>")
def get_persona(persona_name):
    """Get a specific persona configuration."""
    try:
        from core.persona_manager import persona_manager

        # Load the persona data
        persona_data = persona_manager.load_persona(persona_name)
        if not persona_data:
            return jsonify({"error": f"Persona '{persona_name}' not found"}), 404

        # Switch to this persona in the persona manager
        persona_manager.current_persona = persona_name

        # Format the data for the frontend
        result = {
            "PERSONALITY": persona_data.get("personality", {}),
            "BACKSTORY": persona_data.get("backstory", {}),
            "META": persona_data.get("meta", {}),
            "WAKEUP": {},  # Wakeup sounds are handled separately
        }

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/persona/<persona_name>", methods=["DELETE"])
def delete_persona(persona_name):
    """Delete a specific persona."""
    if persona_name == "default":
        return jsonify({"error": "Cannot delete the default persona"}), 400

    try:
        from pathlib import Path

        personas_dir = Path("personas")
        # Check new folder structure first: personas/persona_name/persona.ini
        persona_file = personas_dir / persona_name / "persona.ini"
        if not persona_file.exists():
            # Fall back to old structure: personas/persona_name.ini
            persona_file = personas_dir / f"{persona_name}.ini"

        if not persona_file.exists():
            return jsonify({"error": f"Persona '{persona_name}' not found"}), 404

        # If it's a folder structure, remove the entire folder
        if (
            persona_file.parent.name == persona_name
            and persona_file.name == "persona.ini"
        ):
            import shutil

            shutil.rmtree(persona_file.parent)
        else:
            # Old structure, just remove the file
            persona_file.unlink()

        return jsonify({"message": f"Persona '{persona_name}' deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/persona/switch", methods=["POST"])
def switch_persona():
    """Switch to a different persona (works in guest mode too)."""
    try:
        from core.persona_manager import persona_manager
        from core.profile_manager import user_manager

        data = request.json
        persona_name = data.get("persona_name")

        if not persona_name:
            return jsonify({"error": "persona_name is required"}), 400

        # Validate persona exists
        available_personas = [
            p["name"] for p in persona_manager.get_available_personas()
        ]
        if persona_name not in available_personas:
            return jsonify({"error": f"Persona '{persona_name}' not found"}), 404

        # Switch persona manager to new persona
        persona_manager.switch_persona(persona_name)

        # If there's a current user (not guest), also update their preferred persona
        current_user = user_manager.get_current_user()
        if current_user:
            current_user.set_preferred_persona(persona_name)
        else:
            # Guest mode: save the preferred persona to the guest profile
            try:
                guest_profile = user_manager.identify_user("guest", "high")
                if guest_profile:
                    guest_profile.set_preferred_persona(persona_name)
            except Exception as e:
                print(f"Failed to save guest persona preference: {e}")

        return jsonify({
            "success": True,
            "message": f"Switched to {persona_name} persona",
            "persona_name": persona_name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/persona", methods=["POST"])
def save_persona():
    data = request.json
    persona_name = data.get("persona_name", "default")

    print(
        f"DEBUG: Saving persona '{persona_name}' with wake-up data: {data.get('WAKEUP', {})}"
    )
    print(f"DEBUG: MOUTH_ARTICULATION: {data.get('MOUTH_ARTICULATION', 'NOT_FOUND')}")
    print(f"DEBUG: Full data keys: {list(data.keys())}")

    # Determine the file path based on persona name
    if persona_name == "default":
        persona_file = PERSONA_PATH
    else:
        from pathlib import Path

        personas_dir = Path("personas")
        # Use new folder structure: personas/persona_name/persona.ini
        persona_file = personas_dir / persona_name / "persona.ini"

    print(f"DEBUG: Saving to file: {persona_file}")

    config = configparser.ConfigParser()
    config["PERSONALITY"] = {k: str(v) for k, v in data.get("PERSONALITY", {}).items()}
    config["BACKSTORY"] = data.get("BACKSTORY", {})

    # Handle META section - can be string or object
    meta_data = data.get("META", "")
    if isinstance(meta_data, dict):
        # META is an object with name, description, instructions, voice
        config["META"] = {
            "name": meta_data.get("name", ""),
            "description": meta_data.get("description", ""),
            "instructions": meta_data.get("instructions", ""),
            "voice": meta_data.get("voice", data.get("VOICE", "ballad")),
            "mouth_articulation": meta_data.get(
                "mouth_articulation", data.get("MOUTH_ARTICULATION", "5")
            ),
        }
    else:
        # META is a string (instructions only)
        config["META"] = {
            "instructions": meta_data,
            "voice": data.get("VOICE", "ballad"),
            "mouth_articulation": data.get("MOUTH_ARTICULATION", "5"),
        }

    print(f"DEBUG: META section being written: {config['META']}")
    wakeup = data.get("WAKEUP", {})
    config["WAKEUP"] = {
        str(k): v["text"] if isinstance(v, dict) and "text" in v else str(v)
        for k, v in wakeup.items()
    }
    wakeup_moods = {}
    for k, v in wakeup.items():
        if isinstance(v, dict):
            moods = _normalize_wakeup_moods(v.get("moods", []))
            if moods:
                wakeup_moods[str(k)] = moods
    if wakeup_moods:
        config["WAKEUP_MOODS"] = wakeup_moods

    # Ensure the personas directory exists
    if persona_name != "default":
        persona_file.parent.mkdir(exist_ok=True)

    with open(persona_file, "w") as f:
        config.write(f)

    # Clear the persona cache so fresh data is loaded next time
    from core.persona_manager import persona_manager

    persona_manager.clear_persona_cache(persona_name)

    return jsonify({"status": "ok"})


@bp.route("/persona/wakeup", methods=["POST"])
def save_single_wakeup_phrase():
    data = request.get_json()
    index = str(data.get("index"))
    phrase = data.get("phrase", "").strip()
    moods = _normalize_wakeup_moods(data.get("moods", []))
    requested_persona = str(data.get("persona") or "").strip()
    if not index or not phrase:
        return jsonify({"error": "Missing index or phrase"}), 400

    # Get current persona to determine which file to save to
    current_persona = requested_persona or "default"
    if not requested_persona:
        try:
            from core.persona_manager import persona_manager

            current_persona = persona_manager.current_persona
        except Exception:
            pass

    # Determine the file path based on current persona
    if current_persona == "default":
        persona_file = PERSONA_PATH
    else:
        from pathlib import Path

        personas_dir = Path("personas")
        persona_file = personas_dir / current_persona / "persona.ini"
        # Ensure the directory exists
        persona_file.parent.mkdir(exist_ok=True)

    config = configparser.ConfigParser()
    config.read(persona_file)
    if "WAKEUP" not in config:
        config["WAKEUP"] = {}
    config["WAKEUP"][index] = phrase
    if moods:
        if "WAKEUP_MOODS" not in config:
            config["WAKEUP_MOODS"] = {}
        config["WAKEUP_MOODS"][index] = moods
    elif "WAKEUP_MOODS" in config and index in config["WAKEUP_MOODS"]:
        del config["WAKEUP_MOODS"][index]
    with open(persona_file, "w") as f:
        config.write(f)
    return jsonify({"status": "ok"})


@bp.route('/persona/import', methods=['POST'])
def import_persona():
    if request.is_json:
        ini = request.json.get('ini', '')
    elif 'file' in request.files:
        ini = request.files['file'].read().decode('utf-8')
    else:
        return jsonify({'error': 'No file provided'}), 400
    if not ini or '[PERSONALITY]' not in ini:
        return jsonify({'error': 'Invalid INI file'}), 400
    try:
        with open(PERSONA_PATH, 'w') as f:
            f.write(ini)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/persona/export/<persona_name>')
def export_persona_by_name(persona_name):
    """Export a specific persona by name."""

    try:
        if persona_name == "default":
            persona_file = PERSONA_PATH
        else:
            # Use the project root personas directory, not relative to webconfig/app
            from ..state import PROJECT_ROOT

            personas_dir = PROJECT_ROOT / "personas"
            persona_file = personas_dir / persona_name / "persona.ini"

        print(f"DEBUG: Exporting persona '{persona_name}' from file: {persona_file}")
        print(f"DEBUG: File exists: {persona_file.exists()}")
        print(f"DEBUG: Absolute path: {persona_file.absolute()}")

        if not persona_file.exists():
            return jsonify({'error': f'Persona not found: {persona_file}'}), 404

        return send_file(
            str(persona_file.absolute()),
            as_attachment=True,
            download_name=f"{persona_name}.ini",
            mimetype="text/plain",
        )
    except Exception as e:
        print(f"ERROR: Export failed for persona '{persona_name}': {str(e)}")
        return jsonify({'error': f'Export failed: {str(e)}'}), 500


@bp.route('/persona/import/<persona_name>', methods=['POST'])
def import_persona_by_name(persona_name):
    """Import a persona file to a specific persona name."""

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    try:
        ini_content = file.read().decode('utf-8')
        if not ini_content or '[PERSONALITY]' not in ini_content:
            return jsonify({'error': 'Invalid INI file'}), 400

        # Determine target file path
        if persona_name == "default":
            target_file = PERSONA_PATH
        else:
            # Use the project root personas directory, not relative to webconfig/app
            from ..state import PROJECT_ROOT

            personas_dir = PROJECT_ROOT / "personas"
            target_file = personas_dir / persona_name / "persona.ini"
            target_file.parent.mkdir(exist_ok=True)

        # Write the imported content
        with open(target_file, 'w') as f:
            f.write(ini_content)

        # Clear the persona cache
        from core.persona_manager import persona_manager

        persona_manager.clear_persona_cache(persona_name)

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/persona/export')
def export_persona():
    return send_file(
        PERSONA_PATH,
        as_attachment=True,
        download_name="persona.ini",
        mimetype="text/plain",
    )


@bp.route('/persona/presets', methods=['GET'])
def get_persona_presets():
    """Get list of available persona preset templates."""
    try:
        from core.persona_manager import persona_manager

        presets = persona_manager.get_persona_presets()
        return jsonify(presets)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route('/persona/create-from-preset', methods=['POST'])
def create_persona_from_preset():
    """Create a new persona from a persona preset template."""
    try:
        data = request.get_json()
        preset_id = data.get('preset_id')
        persona_name = data.get('persona_name')

        if not preset_id or not persona_name:
            return jsonify({"error": "preset_id and persona_name are required"}), 400

        # Clean the persona name for folder name: lowercase and remove special characters
        import re

        clean_name = re.sub(r'[^a-z0-9\-_]', '', persona_name.strip().lower())

        if not clean_name:
            return jsonify({"error": "Invalid persona name"}), 400

        from core.persona_manager import persona_manager

        # Pass both the original name (for display) and clean name (for folder)
        success = persona_manager.create_persona_from_preset(
            preset_id, clean_name, display_name=persona_name.strip()
        )

        if success:
            return jsonify({"status": "ok", "persona_name": clean_name})
        return jsonify({"error": "Failed to create persona from preset"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500
