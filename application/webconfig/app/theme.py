"""The device's names and images in the web interface (billy_runtime.theme)."""

from flask import Blueprint, current_app, send_file, send_from_directory


bp = Blueprint("theme", __name__)


def device_name(current=None) -> str:
    """The hostname as the header shows it (billy_runtime.theme.display_name)."""
    from billy_runtime import theme

    return theme.display_name(current)


@bp.route("/theme/<path:filename>")
def themed_file(filename):
    """A themeable file: the device's version, else the core's static file."""
    from billy_runtime import theme

    override = theme.asset(filename)
    if override is not None:
        response = send_file(override, max_age=300)
    else:
        response = send_from_directory(current_app.static_folder, filename, max_age=300)
    return response
