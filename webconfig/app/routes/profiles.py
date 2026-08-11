"""
Profile management routes for Billy Bass Assistant.
Handles user profile CRUD operations and persona management.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Blueprint, jsonify, request, send_file

from core.env_utils import set_env_key


def get_profiles_dir():
    """Get the absolute path to the profiles directory."""
    current_dir = Path(__file__).parent
    project_root = (
        current_dir.parent.parent.parent
    )  # Go up 3 levels: routes -> app -> webconfig -> project root
    return project_root / "profiles"


def _get_env_path() -> Path:
    """Return absolute .env path in project root."""
    current_dir = Path(__file__).parent
    project_root = current_dir.parent.parent.parent
    return project_root / ".env"


profiles_bp = Blueprint('profiles', __name__)


@profiles_bp.route('/profiles', methods=['GET'])
def list_profiles():
    """List all available user profiles."""
    try:
        profiles_dir = get_profiles_dir()
        if not profiles_dir.exists():
            return jsonify({"profiles": []})

        profiles = []
        for profile_file in profiles_dir.glob("*.ini"):
            profile_name = profile_file.stem.title()  # Convert to title case

            # Read display name from profile
            display_name = profile_name  # Default to profile name
            try:
                import configparser

                config = configparser.ConfigParser()
                config.read(profile_file)
                if config.has_section("USER_INFO") and config.has_option(
                    "USER_INFO", "display_name"
                ):
                    display_name = config.get("USER_INFO", "display_name")
            except Exception:
                pass  # Use default display name if reading fails

            profiles.append({
                "name": profile_name,
                "display_name": display_name,
                "file": str(profile_file),
                "size": profile_file.stat().st_size,
                "modified": profile_file.stat().st_mtime,
            })

        return jsonify({"profiles": profiles})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/<profile_name>', methods=['GET'])
def get_profile(profile_name):
    """Get details of a specific profile."""
    try:
        # Convert to lowercase to match file naming convention
        profile_file = get_profiles_dir() / f"{profile_name.lower()}.ini"
        if not profile_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        # Read profile data
        import configparser

        config = configparser.ConfigParser()
        config.read(profile_file)

        profile_data = {}
        for section in config.sections():
            profile_data[section] = dict(config.items(section))

        return jsonify({"name": profile_name, "data": profile_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/<profile_name>', methods=['DELETE'])
def delete_profile(profile_name):
    """Delete a user profile."""
    try:
        # Convert to lowercase to match file naming convention
        profile_file = get_profiles_dir() / f"{profile_name.lower()}.ini"
        if not profile_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        profile_file.unlink()
        return jsonify({"message": f"Profile {profile_name} deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/personas', methods=['GET'])
def list_personas():
    """List all available personas."""
    try:
        personas_dir = Path("personas")
        if not personas_dir.exists():
            return jsonify({"personas": []})

        personas = []
        for persona_file in personas_dir.glob("*.ini"):
            persona_name = persona_file.stem

            # Read persona metadata
            import configparser

            config = configparser.ConfigParser()
            config.read(persona_file)

            meta = dict(config.items("META")) if config.has_section("META") else {}
            personality = (
                dict(config.items("PERSONALITY"))
                if config.has_section("PERSONALITY")
                else {}
            )

            personas.append({
                "name": persona_name,
                "description": meta.get("description", persona_name),
                "mood": meta.get("mood", "neutral"),
                "energy": meta.get("energy", "medium"),
                "personality_traits": personality,
            })

        return jsonify({"personas": personas})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/current-user', methods=['GET'])
def get_current_user():
    """Get the currently active user profile."""
    try:
        # Import here to avoid circular imports
        from core.config import DEFAULT_USER
        from core.persona_manager import persona_manager
        from core.profile_manager import user_manager

        env_path = _get_env_path()
        load_dotenv(env_path, override=True)
        current_user_env = os.getenv("CURRENT_USER", "").strip().strip("'\"")

        current_user = user_manager.get_current_user()

        # Prefer CURRENT_USER from .env when set
        if (
            not current_user
            and current_user_env
            and current_user_env.lower() != "guest"
        ):
            try:
                current_user = user_manager.identify_user(current_user_env, "high")
            except Exception as e:
                print(f"Failed to load current user {current_user_env}: {e}")

        # Only fallback to DEFAULT_USER when CURRENT_USER is empty
        if (
            not current_user
            and not current_user_env
            and DEFAULT_USER
            and DEFAULT_USER.lower() != "guest"
        ):
            try:
                current_user = user_manager.identify_user(DEFAULT_USER, "high")
            except Exception as e:
                print(f"Failed to load default user {DEFAULT_USER}: {e}")

        if not current_user:
            return jsonify({"user": None})

        # Keep persona manager aligned with loaded user's preference.
        preferred_persona = current_user.data.get("USER_INFO", {}).get(
            "preferred_persona", "default"
        )
        persona_manager.switch_persona(preferred_persona)

        return jsonify({
            "user": {
                "name": current_user.name,
                "data": current_user.data,
                "memories": current_user.get_memories(10),  # Last 10 memories
                "context": current_user.get_context_string(),
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/current-user', methods=['POST'])
def set_current_user():
    """Set the current user profile."""
    try:
        data = request.get_json()
        user_name = data.get("name", "").strip()

        if not user_name:
            return jsonify({"error": "User name is required"}), 400

        # Import here to avoid circular imports
        from core.persona_manager import persona_manager
        from core.profile_manager import user_manager

        # Identify the user (this will load or create the profile)
        profile = user_manager.identify_user(user_name, "high")

        if profile:
            # Persist CURRENT_USER for other processes (main Billy service).
            env_path = _get_env_path()
            set_env_key(str(env_path), "CURRENT_USER", profile.name.lower())

            # Switch persona manager to this profile's preferred persona.
            preferred_persona = profile.data.get("USER_INFO", {}).get(
                "preferred_persona", "default"
            )
            persona_manager.switch_persona(preferred_persona)

            return jsonify({
                "message": f"Switched to user: {user_name}",
                "user": {"name": profile.name, "data": profile.data},
            })
        return jsonify({"error": "Failed to load user profile"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/current-user', methods=['DELETE'])
def clear_current_user():
    """Clear the current user profile (switch to guest mode)."""
    try:
        # Import here to avoid circular imports
        from core.profile_manager import user_manager

        user_manager.clear_current_user()
        env_path = _get_env_path()
        set_env_key(str(env_path), "CURRENT_USER", "guest")
        return jsonify({"message": "Current user cleared, switched to guest mode"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/current-user', methods=['PATCH'])
def update_current_user():
    """Update current user settings (like preferred persona)."""
    try:
        data = request.get_json()
        action = data.get("action")

        # Import here to avoid circular imports
        from core.profile_manager import user_manager

        current_user = user_manager.get_current_user()
        if not current_user:
            return jsonify({"error": "No current user"}), 400

        if action == "switch_persona":
            preferred_persona = data.get("preferred_persona")
            if preferred_persona:
                current_user.set_preferred_persona(preferred_persona)
                # Also switch the persona manager to the new persona
                from core.persona_manager import persona_manager

                persona_manager.switch_persona(preferred_persona)
                return jsonify({
                    "message": f"Updated {current_user.name}'s preferred persona to {preferred_persona}"
                })
            return jsonify({"error": "preferred_persona is required"}), 400

        if action == "update_profile":
            # Update both display name and preferred persona in one action
            preferred_persona = data.get("preferred_persona")
            display_name = data.get("display_name")

            if preferred_persona:
                current_user.set_preferred_persona(preferred_persona)
                # Also switch the persona manager to the new persona
                from core.persona_manager import persona_manager

                persona_manager.switch_persona(preferred_persona)

            if display_name:
                current_user.set_display_name(display_name)

            return jsonify({"message": f"Updated {current_user.name}'s profile"})

        return jsonify({"error": "Unknown action"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/rename', methods=['POST'])
def rename_profile():
    """Rename a user profile."""
    try:
        data = request.get_json()
        old_name = data.get("oldName", "").strip()
        new_name = data.get("newName", "").strip()

        if not old_name or not new_name:
            return jsonify({"error": "Both oldName and newName are required"}), 400

        if old_name.lower() == new_name.lower():
            return jsonify({"error": "New name must be different from old name"}), 400

        # Convert to lowercase for file operations
        old_file = get_profiles_dir() / f"{old_name.lower()}.ini"
        new_file = get_profiles_dir() / f"{new_name.lower()}.ini"

        if not old_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        if new_file.exists():
            return jsonify({"error": "A profile with this name already exists"}), 400

        # Rename the file
        old_file.rename(new_file)

        # Update the profile name inside the file
        import configparser

        config = configparser.ConfigParser()
        config.read(new_file)

        if config.has_section("USER_INFO"):
            config.set("USER_INFO", "name", new_name)

            with open(new_file, 'w') as f:
                config.write(f)

        return jsonify({"message": f"Profile renamed from {old_name} to {new_name}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/delete-memory', methods=['POST'])
def delete_memory():
    """Delete a specific memory from a user profile using memory ID."""
    try:
        data = request.get_json()
        user_name = data.get("user", "").strip()
        memory_id = data.get("memoryId", "").strip()

        if not user_name or not memory_id:
            return jsonify({"error": "Both user and memoryId are required"}), 400

        # Convert to lowercase for file operations
        profile_file = get_profiles_dir() / f"{user_name.lower()}.ini"

        if not profile_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        # Read the profile
        import configparser

        config = configparser.ConfigParser()
        config.read(profile_file)

        if not config.has_section("CORE_MEMORIES"):
            return jsonify({"error": "No memories found"}), 404

        # Parse existing memories
        memories_str = config.get("CORE_MEMORIES", "memories", fallback="[]")
        try:
            memories = json.loads(memories_str)
        except json.JSONDecodeError as e:
            print(f"JSON decode error in delete_memory: {e}")
            print(f"Malformed memories string: {memories_str}")
            return jsonify({"error": f"Invalid memories format: {str(e)}"}), 500

        # Find and remove the memory with matching ID
        original_count = len(memories)
        print(f"DEBUG: Original memory count: {original_count}")
        print(f"DEBUG: Looking for memory ID: {memory_id}")
        print(f"DEBUG: Available memory IDs: {[m.get('id') for m in memories]}")

        # Handle both real IDs and temporary IDs (for backward compatibility)
        if memory_id.startswith('temp_'):
            # For temporary IDs, use the date part to match
            date_part = memory_id.replace('temp_', '')
            print(f"DEBUG: Using date-based matching for temp ID: {date_part}")
            memories = [m for m in memories if m.get("date") != date_part]
        else:
            # For real IDs, match by ID
            print(f"DEBUG: Using ID-based matching")
            memories = [m for m in memories if m.get("id") != memory_id]

        new_count = len(memories)
        print(f"DEBUG: New memory count: {new_count}")

        if new_count == original_count:
            print(f"DEBUG: No memory was removed - memory not found")
            return jsonify({"error": "Memory not found"}), 404

        # Update the profile
        config.set("CORE_MEMORIES", "memories", json.dumps(memories))

        with open(profile_file, 'w') as f:
            config.write(f)

        return jsonify({"message": "Memory deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/update-memory', methods=['POST'])
def update_memory():
    """Update a specific memory in a user profile using memory ID."""
    try:
        data = request.get_json()
        user_name = data.get("user", "").strip()
        memory_id = data.get("memoryId", "").strip()
        new_memory = data.get("memory", "").strip()
        new_category = data.get("category", "fact").strip()
        new_importance = data.get("importance", "medium").strip()

        if not user_name or not memory_id or not new_memory:
            return jsonify({"error": "User, memoryId, and memory are required"}), 400

        # Convert to lowercase for file operations
        profile_file = get_profiles_dir() / f"{user_name.lower()}.ini"

        if not profile_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        # Read the profile
        import configparser

        config = configparser.ConfigParser()
        config.read(profile_file)

        if not config.has_section("CORE_MEMORIES"):
            return jsonify({"error": "No memories found"}), 404

        # Parse existing memories
        memories_str = config.get("CORE_MEMORIES", "memories", fallback="[]")
        try:
            memories = json.loads(memories_str)
        except json.JSONDecodeError as e:
            print(f"JSON decode error in update_memory: {e}")
            print(f"Malformed memories string: {memories_str}")
            return jsonify({"error": f"Invalid memories format: {str(e)}"}), 500

        # Find and update the memory with matching ID
        memory_found = False

        # Handle both real IDs and temporary IDs (for backward compatibility)
        if memory_id.startswith('temp_'):
            # For temporary IDs, use the date part to match
            date_part = memory_id.replace('temp_', '')
            for memory in memories:
                if memory.get("date") == date_part:
                    memory["memory"] = new_memory
                    memory["category"] = new_category
                    memory["importance"] = new_importance
                    memory_found = True
                    break
        else:
            # For real IDs, match by ID
            for memory in memories:
                if memory.get("id") == memory_id:
                    memory["memory"] = new_memory
                    memory["category"] = new_category
                    memory["importance"] = new_importance
                    memory_found = True
                    break

        if not memory_found:
            return jsonify({"error": "Memory not found"}), 404

        # Update the profile
        config.set("CORE_MEMORIES", "memories", json.dumps(memories))

        with open(profile_file, 'w') as f:
            config.write(f)

        return jsonify({"message": "Memory updated successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/save-memories', methods=['POST'])
def save_memories():
    """Save all memories for a user profile (overwrites existing memories)."""
    try:
        data = request.get_json()
        user_name = data.get("user", "").strip()
        memories = data.get("memories", [])

        if not user_name:
            return jsonify({"error": "User is required"}), 400

        # Convert to lowercase for file operations
        profile_file = get_profiles_dir() / f"{user_name.lower()}.ini"

        if not profile_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        # Read the profile
        import configparser

        config = configparser.ConfigParser()
        config.read(profile_file)

        # Update the memories
        config.set("CORE_MEMORIES", "memories", json.dumps(memories))

        with open(profile_file, 'w') as f:
            config.write(f)

        return jsonify({"message": f"Saved {len(memories)} memories successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/config', methods=['GET'])
def get_config():
    """Get current configuration including DEFAULT_USER."""
    try:
        return jsonify({"DEFAULT_USER": os.getenv("DEFAULT_USER", "guest")})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/update-display-name', methods=['POST'])
def update_display_name():
    """Update display name for a user's profile."""
    try:
        data = request.json
        user = data.get('user')
        display_name = data.get('display_name', '')

        print(
            f"DEBUG: update_display_name called - user: {user}, display_name: {display_name}"
        )

        if not user:
            return jsonify({"error": "User is required"}), 400

        # Prevent editing Guest profile display name
        if user.lower() == 'guest':
            return jsonify({"error": "Cannot edit Guest profile display name"}), 400

        profile_file = get_profiles_dir() / f"{user.lower()}.ini"
        print(f"DEBUG: update_display_name - profile_file: {profile_file}")
        print(f"DEBUG: update_display_name - file exists: {profile_file.exists()}")
        if not profile_file.exists():
            return jsonify({"error": "Profile not found"}), 404

        # Read current profile
        import configparser

        config = configparser.ConfigParser()
        config.read(profile_file)

        # Ensure USER_INFO section exists
        if not config.has_section('USER_INFO'):
            config.add_section('USER_INFO')

        # Update display name
        config.set('USER_INFO', 'display_name', display_name)

        # Write back to file
        with open(profile_file, 'w') as f:
            config.write(f)

        return jsonify({"message": f"Updated display name for {user}'s profile"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@profiles_bp.route('/profiles/export/<profile_name>')
def export_profile(profile_name):
    """Export a user profile by name."""
    profiles_dir = get_profiles_dir()
    profile_file = profiles_dir / f"{profile_name.lower()}.ini"

    print(f"DEBUG: Exporting profile {profile_name}")
    print(f"DEBUG: Profiles dir: {profiles_dir}")
    print(f"DEBUG: Looking for file: {profile_file}")
    print(f"DEBUG: File exists: {profile_file.exists()}")

    if not profile_file.exists():
        print(f"DEBUG: Profile file not found: {profile_file}")
        return jsonify({'error': 'Profile not found'}), 404

    return send_file(
        str(profile_file),
        as_attachment=True,
        download_name=f"{profile_name}.ini",
        mimetype="text/plain",
    )


@profiles_bp.route('/profiles/import/<profile_name>', methods=['POST'])
def import_profile(profile_name):
    """Import a profile file to a specific profile name."""

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400

    try:
        ini_content = file.read().decode('utf-8')
        if not ini_content or '[USER_INFO]' not in ini_content:
            return jsonify({'error': 'Invalid profile file'}), 400

        # Determine target file path
        profiles_dir = get_profiles_dir()
        target_file = profiles_dir / f"{profile_name.lower()}.ini"
        profiles_dir.mkdir(exist_ok=True)

        # Write the imported content
        with open(target_file, 'w') as f:
            f.write(ini_content)

        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
