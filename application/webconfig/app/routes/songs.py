"""
Flask routes for song management
"""

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename


songs_bp = Blueprint('songs', __name__)


@songs_bp.route('/songs', methods=['GET'])
def list_songs():
    """List all available songs."""
    try:
        from core.song_manager import song_manager

        songs = song_manager.list_songs()
        return jsonify(songs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/<song_name>', methods=['GET'])
def get_song(song_name):
    """Get metadata for a specific song."""
    try:
        from core.song_manager import song_manager

        metadata = song_manager.get_song_metadata(song_name)

        if not metadata:
            return jsonify({"error": "Song not found"}), 404

        return jsonify(metadata)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs', methods=['POST'])
def create_song():
    """Create a new song."""
    try:
        from core.song_manager import song_manager

        data = request.get_json()
        song_name = data.get('name')

        if not song_name:
            return jsonify({"error": "Song name is required"}), 400

        # Sanitize song name
        song_name = secure_filename(song_name).replace(' ', '_').lower()

        # Create song with metadata
        success = song_manager.create_song(song_name, data)

        if not success:
            return jsonify({"error": "Song already exists or failed to create"}), 400

        return jsonify({
            "message": f"Song '{song_name}' created successfully",
            "name": song_name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/<song_name>', methods=['PUT'])
def update_song(song_name):
    """Update song metadata."""
    try:
        from core.song_manager import song_manager

        data = request.get_json()

        # Check if song exists
        if not song_manager.get_song_metadata(song_name):
            return jsonify({"error": "Song not found"}), 404

        # Update metadata
        success = song_manager.save_song_metadata(song_name, data)

        if not success:
            return jsonify({"error": "Failed to update song"}), 500

        return jsonify({"message": f"Song '{song_name}' updated successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/<song_name>/enabled', methods=['PUT'])
def set_song_enabled(song_name):
    """Toggle whether a song is eligible for Billy to pick/play on its own.

    Only touches the 'enabled' field - reads the song's full existing
    metadata first and re-saves it as a whole, since save_song_metadata()
    writes a complete metadata.ini section (a partial dict would silently
    reset every other field to its default).
    """
    try:
        from core.song_manager import song_manager

        data = request.get_json() or {}
        if 'enabled' not in data:
            return jsonify({"error": "'enabled' is required"}), 400

        metadata = song_manager.get_song_metadata(song_name, is_custom=True)
        if not metadata:
            return jsonify({"error": "Song not found"}), 404

        metadata['enabled'] = bool(data['enabled'])
        success = song_manager.save_song_metadata(song_name, metadata)

        if not success:
            return jsonify({"error": "Failed to update song"}), 500

        return jsonify({
            "message": f"Song '{song_name}' enabled={metadata['enabled']}",
            "enabled": metadata['enabled'],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/export/<song_name>', methods=['GET'])
def export_song(song_name):
    """Download a custom song as a zip bundle (metadata.ini + whichever
    audio files it has)."""
    try:
        import io

        from core.song_manager import song_manager

        zip_bytes = song_manager.export_song_zip(song_name)
        if zip_bytes is None:
            return jsonify({"error": "Song not found"}), 404

        return send_file(
            io.BytesIO(zip_bytes),
            mimetype='application/zip',
            as_attachment=True,
            download_name=f"{song_name}.zip",
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/import', methods=['POST'])
def import_song():
    """Create (or overwrite) a custom song from an uploaded zip bundle.

    Unlike persona/profile import, the target name isn't chosen up front -
    a song bundle covers several files at once (not one you're already
    editing), so the name is derived from the uploaded zip's own filename,
    the same way a brand new song's name is derived on creation.
    """
    try:
        from pathlib import Path

        from core.song_manager import song_manager

        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        if not file.filename.lower().endswith('.zip'):
            return jsonify({"error": "File must be a .zip"}), 400

        song_name = secure_filename(Path(file.filename).stem).replace(' ', '_').lower()
        if not song_name:
            return jsonify({
                "error": "Could not derive a song name from that file name"
            }), 400

        success, message = song_manager.import_song_zip(song_name, file.read())

        if not success:
            return jsonify({"error": message}), 400

        return jsonify({"message": message, "name": song_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/<song_name>', methods=['DELETE'])
def delete_song(song_name):
    """Delete a song."""
    try:
        from core.song_manager import song_manager

        success = song_manager.delete_song(song_name)

        if not success:
            return jsonify({"error": "Song not found or failed to delete"}), 404

        return jsonify({"message": f"Song '{song_name}' deleted successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/<song_name>/upload/<file_type>', methods=['POST'])
def upload_audio_file(song_name, file_type):
    """Upload an audio file for a song (full, vocals, or drums)."""
    try:
        from core.song_manager import song_manager

        if file_type not in ['full', 'vocals', 'drums']:
            return jsonify({
                "error": "Invalid file type. Must be 'full', 'vocals', or 'drums'"
            }), 400

        if 'file' not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400

        if not file.filename.lower().endswith(('.wav', '.mp3', '.m4a')):
            return jsonify({"error": "File must be a WAV, MP3, or M4A file"}), 400

        # Read file data
        file_data = file.read()

        # Save audio file (MP3/M4A are transcoded to WAV automatically)
        success = song_manager.save_audio_file(
            song_name, file_type, file_data, original_filename=file.filename
        )

        if not success:
            return jsonify({"error": f"Failed to save {file_type}.wav"}), 500

        return jsonify({
            "message": f"Uploaded {file_type}.wav for '{song_name}' successfully"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/copy-example/<example_name>', methods=['POST'])
def copy_example_song(example_name):
    """Copy an example song to custom_songs directory."""
    try:
        from core.song_manager import song_manager

        data = request.get_json() or {}
        new_name = data.get('new_name', example_name)

        success = song_manager.copy_example_to_custom(example_name, new_name)

        if not success:
            return jsonify({"error": "Failed to copy example song"}), 400

        return jsonify({
            "message": f"Copied example song '{example_name}' to custom songs as '{new_name}'",
            "name": new_name,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@songs_bp.route('/songs/<song_name>/<file_type>.wav', methods=['GET'])
def serve_audio_file(song_name, file_type):
    """Serve an audio file for a song (full, vocals, or drums)."""
    try:
        from core.song_manager import song_manager

        if file_type not in ['full', 'vocals', 'drums']:
            return jsonify({
                "error": "Invalid file type. Must be 'full', 'vocals', or 'drums'"
            }), 400

        # Get the file path
        file_path = song_manager.get_audio_file_path(song_name, file_type)

        if not file_path or not file_path.exists():
            return jsonify({"error": f"Audio file not found: {file_type}.wav"}), 404

        return send_file(str(file_path), mimetype='audio/wav', as_attachment=False)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
