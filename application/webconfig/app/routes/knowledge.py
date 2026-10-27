"""Routes for Billy's lightweight local knowledge base."""

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename


knowledge_bp = Blueprint("knowledge", __name__)


@knowledge_bp.route("/knowledge/folders", methods=["GET"])
def list_knowledge_folders():
    """List knowledge folders with summary and documents."""
    try:
        from core.knowledge_manager import knowledge_manager

        return jsonify({
            "folders": knowledge_manager.list_folders(include_documents=True),
            "summary": knowledge_manager.get_summary(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@knowledge_bp.route("/knowledge/folders", methods=["POST"])
def create_knowledge_folder():
    """Create a new knowledge folder."""
    try:
        from core.knowledge_manager import knowledge_manager

        data = request.get_json(silent=True) or {}
        folder = knowledge_manager.create_folder(
            label=str(data.get("label") or "").strip(),
            trigger_topics=data.get("trigger_topics"),
        )
        return jsonify({
            "ok": True,
            "folder": folder,
            "folders": knowledge_manager.list_folders(include_documents=True),
            "summary": knowledge_manager.get_summary(),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@knowledge_bp.route("/knowledge/folders/<folder_id>", methods=["PATCH"])
def update_knowledge_folder(folder_id: str):
    """Update knowledge folder settings."""
    try:
        from core.knowledge_manager import knowledge_manager

        data = request.get_json(silent=True) or {}
        folder = knowledge_manager.update_folder(
            folder_id,
            label=data.get("label"),
            trigger_topics=data.get("trigger_topics"),
        )
        return jsonify({
            "ok": True,
            "folder": folder,
            "folders": knowledge_manager.list_folders(include_documents=True),
            "summary": knowledge_manager.get_summary(),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@knowledge_bp.route("/knowledge/folders/<folder_id>", methods=["DELETE"])
def delete_knowledge_folder(folder_id: str):
    """Delete a knowledge folder."""
    try:
        from core.knowledge_manager import knowledge_manager

        if not knowledge_manager.delete_folder(folder_id):
            return jsonify({"error": "Folder not found"}), 404
        return jsonify({
            "ok": True,
            "folders": knowledge_manager.list_folders(include_documents=True),
            "summary": knowledge_manager.get_summary(),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@knowledge_bp.route("/knowledge/documents", methods=["POST"])
def upload_knowledge_document():
    """Upload a new knowledge document and rebuild the local index."""
    try:
        from core.knowledge_manager import knowledge_manager

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "No file selected"}), 400

        filename = secure_filename(file.filename)
        if not filename:
            return jsonify({"error": "Invalid filename"}), 400

        folder_id = str(request.form.get("folder_id") or "").strip()
        if not folder_id:
            return jsonify({"error": "Folder is required"}), 400

        document = knowledge_manager.add_document(
            folder_id=folder_id,
            filename=filename,
            data=file.read(),
        )
        return jsonify({
            "ok": True,
            "document": document,
            "folders": knowledge_manager.list_folders(include_documents=True),
            "summary": knowledge_manager.get_summary(),
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@knowledge_bp.route("/knowledge/documents/<document_id>", methods=["DELETE"])
def delete_knowledge_document(document_id):
    """Delete a knowledge document."""
    try:
        from core.knowledge_manager import knowledge_manager

        if not knowledge_manager.delete_document(document_id):
            return jsonify({"error": "Document not found"}), 404
        return jsonify({
            "ok": True,
            "folders": knowledge_manager.list_folders(include_documents=True),
            "summary": knowledge_manager.get_summary(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@knowledge_bp.route("/knowledge/reindex", methods=["POST"])
def rebuild_knowledge_index():
    """Force a rebuild of the knowledge chunk index."""
    try:
        from core.knowledge_manager import knowledge_manager

        data = request.get_json(silent=True) or {}
        folder_id = str(data.get("folder_id") or "").strip() or None
        result = knowledge_manager.rebuild_index(folder_id=folder_id)
        result["folders"] = knowledge_manager.list_folders(include_documents=True)
        result["summary"] = knowledge_manager.get_summary()
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
