"""
Song Manager - Handles custom song management for Billy Bass
"""

import configparser
import shutil
from pathlib import Path
from typing import Any

from .logger import logger


class SongManager:
    """Manages custom songs and their metadata."""

    def __init__(self):
        # Get the project root directory (where this file is located)
        project_root = Path(__file__).parent.parent

        # Primary location for custom songs (git ignored)
        self.custom_songs_dir = project_root / "custom_songs"
        self.custom_songs_dir.mkdir(parents=True, exist_ok=True)

        # Example songs location (in git)
        self.example_songs_dir = project_root / "sounds" / "songs"

        # For backward compatibility and general operations, use custom_songs as default
        self.songs_dir = self.custom_songs_dir

    def list_songs(self) -> list[dict[str, Any]]:
        """List all available songs with their metadata from both custom and example directories."""
        songs = []
        seen_names = set()

        # First, get custom songs (priority)
        if self.custom_songs_dir.exists():
            for song_dir in self.custom_songs_dir.iterdir():
                if song_dir.is_dir():
                    metadata = self.get_song_metadata(song_dir.name, is_custom=True)
                    if metadata:
                        songs.append(metadata)
                        seen_names.add(song_dir.name)

        # Then, get example songs (only if not already in custom)
        if self.example_songs_dir.exists():
            for song_dir in self.example_songs_dir.iterdir():
                if song_dir.is_dir() and song_dir.name not in seen_names:
                    metadata = self.get_song_metadata(song_dir.name, is_custom=False)
                    if metadata:
                        metadata['is_example'] = True
                        songs.append(metadata)

        return sorted(songs, key=lambda x: x.get('title', x['name']))

    def get_song_metadata(
        self, song_name: str, is_custom: bool | None = None
    ) -> dict[str, Any] | None:
        """Get metadata for a specific song.

        Args:
            song_name: Name of the song
            is_custom: If True, look in custom_songs; if False, look in sounds/songs;
                      if None, check custom first, then example
        """
        if is_custom is None:
            # Check custom first, then example
            song_path = self.custom_songs_dir / song_name
            if not song_path.exists():
                song_path = self.example_songs_dir / song_name
                if not song_path.exists():
                    return None
        elif is_custom:
            song_path = self.custom_songs_dir / song_name
            if not song_path.exists():
                return None
        else:
            song_path = self.example_songs_dir / song_name
            if not song_path.exists():
                return None

        metadata_file = song_path / "metadata.ini"

        # Check for required audio files
        has_full = (song_path / "full.wav").exists()
        has_vocals = (song_path / "vocals.wav").exists()
        has_drums = (song_path / "drums.wav").exists()

        # Default metadata
        metadata = {
            "name": song_name,
            "title": song_name.replace('_', ' ').title(),
            "keywords": "",
            "bpm": 120.0,
            "gain": 1.0,
            "tail_threshold": 1500.0,
            "compensate_tail": 0.0,
            "head_moves": "",
            "half_tempo_tail_flap": False,
            "has_full": has_full,
            "has_vocals": has_vocals,
            "has_drums": has_drums,
        }

        # Load from INI file if it exists
        if metadata_file.exists():
            config = configparser.ConfigParser()
            config.read(metadata_file)

            if config.has_section('SONG'):
                metadata.update({
                    "title": config.get('SONG', 'title', fallback=metadata['title']),
                    "keywords": config.get('SONG', 'keywords', fallback=''),
                    "bpm": config.getfloat('SONG', 'bpm', fallback=120.0),
                    "gain": config.getfloat('SONG', 'gain', fallback=1.0),
                    "tail_threshold": config.getfloat(
                        'SONG', 'tail_threshold', fallback=1500.0
                    ),
                    "compensate_tail": config.getfloat(
                        'SONG', 'compensate_tail', fallback=0.0
                    ),
                    "head_moves": config.get('SONG', 'head_moves', fallback=''),
                    "half_tempo_tail_flap": config.getboolean(
                        'SONG', 'half_tempo_tail_flap', fallback=False
                    ),
                })
        # Try to load from old metadata.txt format
        elif (song_path / "metadata.txt").exists():
            old_metadata = self._load_old_metadata(song_path / "metadata.txt")
            metadata.update(old_metadata)

        return metadata

    def _load_old_metadata(self, path: Path) -> dict[str, Any]:
        """Load metadata from old metadata.txt format."""
        metadata = {}

        with open(path) as f:
            for line in f:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    if key == "head_moves":
                        metadata[key] = value
                    elif key in ("bpm", "tail_threshold", "gain", "compensate_tail"):
                        metadata[key] = float(value.strip())
                    elif key == "half_tempo_tail_flap":
                        metadata[key] = value.strip().lower() == "true"

        return metadata

    def save_song_metadata(self, song_name: str, metadata: dict[str, Any]) -> bool:
        """Save metadata for a song in custom_songs."""
        # Always save to custom_songs
        song_path = self.custom_songs_dir / song_name
        song_path.mkdir(parents=True, exist_ok=True)

        metadata_file = song_path / "metadata.ini"
        config = configparser.ConfigParser()

        config['SONG'] = {
            'title': metadata.get('title', song_name.replace('_', ' ').title()),
            'keywords': metadata.get('keywords', ''),
            'bpm': str(metadata.get('bpm', 120.0)),
            'gain': str(metadata.get('gain', 1.0)),
            'tail_threshold': str(metadata.get('tail_threshold', 1500.0)),
            'compensate_tail': str(metadata.get('compensate_tail', 0.0)),
            'head_moves': metadata.get('head_moves', ''),
            'half_tempo_tail_flap': str(metadata.get('half_tempo_tail_flap', False)),
        }

        try:
            with open(metadata_file, 'w') as f:
                config.write(f)
            logger.info(f"Saved metadata for song: {song_name}", "🎵")
            return True
        except Exception as e:
            logger.error(f"Failed to save metadata for {song_name}: {e}")
            return False

    def create_song(self, song_name: str, metadata: dict[str, Any]) -> bool:
        """Create a new song directory with metadata in custom_songs."""
        song_path = self.custom_songs_dir / song_name

        if song_path.exists():
            logger.warning(f"Song already exists: {song_name}")
            return False

        song_path.mkdir(parents=True, exist_ok=True)
        return self.save_song_metadata(song_name, metadata)

    def delete_song(self, song_name: str) -> bool:
        """Delete a song and all its files (only from custom_songs)."""
        # Only allow deleting custom songs, not examples
        song_path = self.custom_songs_dir / song_name

        if not song_path.exists():
            logger.warning(f"Custom song not found: {song_name}")
            return False

        try:
            shutil.rmtree(song_path)
            logger.info(f"Deleted song: {song_name}", "🗑️")
            return True
        except Exception as e:
            logger.error(f"Failed to delete song {song_name}: {e}")
            return False

    def save_audio_file(self, song_name: str, file_type: str, file_data: bytes) -> bool:
        """Save an audio file for a song (full.wav, vocals.wav, or drums.wav) in custom_songs."""
        if file_type not in ['full', 'vocals', 'drums']:
            logger.error(f"Invalid file type: {file_type}")
            return False

        # Always save to custom_songs
        song_path = self.custom_songs_dir / song_name
        song_path.mkdir(parents=True, exist_ok=True)

        audio_file = song_path / f"{file_type}.wav"

        try:
            with open(audio_file, 'wb') as f:
                f.write(file_data)
            logger.info(f"Saved {file_type}.wav for song: {song_name}", "🎵")
            return True
        except Exception as e:
            logger.error(f"Failed to save {file_type}.wav for {song_name}: {e}")
            return False

    def get_audio_file_path(self, song_name: str, file_type: str) -> Path | None:
        """Get the path to an audio file for a song.

        Args:
            song_name: Name of the song
            file_type: Type of audio file ('full', 'vocals', or 'drums')

        Returns:
            Path to the audio file, or None if not found
        """
        if file_type not in ['full', 'vocals', 'drums']:
            return None

        # First check custom_songs
        custom_path = self.custom_songs_dir / song_name / f"{file_type}.wav"
        if custom_path.exists():
            return custom_path

        # Then check example songs
        example_path = self.example_songs_dir / song_name / f"{file_type}.wav"
        if example_path.exists():
            return example_path

        return None

    def copy_example_to_custom(
        self, example_name: str, new_name: str | None = None
    ) -> bool:
        """Copy an example song to custom_songs directory.

        Args:
            example_name: Name of the example song to copy
            new_name: Optional new name for the copied song (defaults to example_name)
        """
        if new_name is None:
            new_name = example_name

        example_path = self.example_songs_dir / example_name
        custom_path = self.custom_songs_dir / new_name

        if not example_path.exists():
            logger.error(f"Example song not found: {example_name}")
            return False

        if custom_path.exists():
            logger.warning(f"Custom song already exists: {new_name}")
            return False

        try:
            shutil.copytree(example_path, custom_path)
            logger.info(
                f"Copied example song '{example_name}' to custom_songs as '{new_name}'",
                "📋",
            )
            return True
        except Exception as e:
            logger.error(f"Failed to copy example song: {e}")
            return False

    def get_dynamic_tool_description(self) -> str:
        """Generate dynamic tool description based on available songs."""
        songs = self.list_songs()

        if not songs:
            return "Plays a special Billy song. No songs are currently available."

        song_list = []
        for song in songs:
            title = song.get('title', song['name'])
            keywords = song.get('keywords', '')
            if keywords:
                song_list.append(f"- '{song['name']}' ({title}): {keywords}")
            else:
                song_list.append(f"- '{song['name']}' ({title})")

        description = (
            "Plays a special Billy song based on the given name. Available songs:\n"
        )
        description += "\n".join(song_list)
        description += "\n\nIMPORTANT: Use the song name (first part in quotes) when calling this function, NOT the title in parentheses."

        return description


# Global instance
song_manager = SongManager()
