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

        if not file.filename.lower().endswith('.wav'):
            return jsonify({"error": "File must be a WAV file"}), 400

        # Read file data
        file_data = file.read()

        # Save audio file
        success = song_manager.save_audio_file(song_name, file_type, file_data)

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


@songs_bp.route('/songs/<song_name>/preprocess', methods=['POST'])
def preprocess_song(song_name):
    """Automatically analyze song audio and generate movement schedules."""
    try:
        from core.song_manager import song_manager

        success = song_manager.preprocess_song_movements(song_name)

        if success:
            return jsonify({
                "success": True,
                "message": f"Successfully preprocessed song '{song_name}'"
            })
        else:
            return jsonify({
                "success": False,
                "error": f"Failed to preprocess song '{song_name}'"
            }), 500

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
