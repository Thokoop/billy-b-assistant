"""
Audio Preprocessor - Automatically analyze songs and generate movement schedules
"""

import os
import numpy as np
import librosa
from scipy.signal import find_peaks
from typing import List, Tuple, Dict, Optional
import configparser
from pathlib import Path

from .logger import logger


class AudioPreprocessor:
    """Preprocesses song audio to automatically generate movement schedules."""

    def __init__(self):
        self.sample_rate = 22050  # Standard sample rate for analysis

    def load_audio(self, file_path: str) -> Tuple[np.ndarray, float]:
        """Load audio file and return audio data and sample rate."""
        try:
            audio, sr = librosa.load(file_path, sr=self.sample_rate)
            return audio, sr
        except Exception as e:
            logger.error(f"Failed to load audio {file_path}: {e}")
            return np.array([]), 0

    def detect_vocal_segments(self, vocals_audio: np.ndarray, sr: float,
                            threshold: float = 0.005, min_duration: float = 0.08,
                            max_gap: float = 0.15) -> List[Tuple[float, float]]:
        """
        Detect vocal segments from vocals.wav for mouth movements.

        Returns: List of (start_time, duration) tuples
        """
        if len(vocals_audio) == 0:
            return []

        # Calculate RMS energy in windows
        frame_length = int(sr * 0.05)  # 50ms windows
        hop_length = int(sr * 0.025)   # 25ms hop

        rms = librosa.feature.rms(y=vocals_audio, frame_length=frame_length,
                                hop_length=hop_length)[0]

        # Find frames above threshold
        active_frames = rms > threshold
        times = librosa.times_like(rms, sr=sr, hop_length=hop_length)

        # Group consecutive active frames
        segments = []
        start_time = None
        last_active_time = 0

        for i, (time, active) in enumerate(zip(times, active_frames)):
            if active:
                if start_time is None:
                    start_time = time
                last_active_time = time
            elif start_time is not None:
                # Check if gap is small enough to continue segment
                if time - last_active_time <= max_gap:
                    continue
                else:
                    # End segment
                    duration = last_active_time - start_time
                    if duration >= min_duration:
                        segments.append((start_time, duration))
                    start_time = None

        # Handle final segment
        if start_time is not None:
            duration = last_active_time - start_time
            if duration >= min_duration:
                segments.append((start_time, duration))

        return segments

    def detect_beats(self, audio: np.ndarray, sr: float,
                   bpm: Optional[float] = None) -> Tuple[List[float], float]:
        """
        Detect beats for head movements.

        Returns: Tuple of (beat_times, estimated_bpm)
        """
        if len(audio) == 0:
            return [], 120.0

        # Use librosa's beat tracking (don't bias with original BPM)
        tempo, beat_positions = librosa.beat.beat_track(y=audio, sr=sr)

        # Convert beat positions to time
        beat_times = librosa.frames_to_time(beat_positions, sr=sr)

        return beat_times.tolist(), float(tempo)

    def filter_significant_beats(self, beat_times: List[float],
                               audio: np.ndarray, sr: float,
                               min_energy_threshold: float = 0.1) -> List[float]:
        """
        Filter beats to keep only significant ones based on energy.
        """
        if not beat_times:
            return []

        significant_beats = []

        for beat_time in beat_times:
            # Get audio around beat time
            start_sample = int((beat_time - 0.05) * sr)
            end_sample = int((beat_time + 0.05) * sr)

            if start_sample < 0 or end_sample >= len(audio):
                continue

            segment = audio[start_sample:end_sample]
            energy = np.sqrt(np.mean(segment ** 2))

            if energy > min_energy_threshold:
                significant_beats.append(beat_time)

        return significant_beats

    def generate_head_moves(self, beat_times: List[float],
                          max_moves: int = 20) -> List[Tuple[float, float]]:
        """
        Generate head movement schedule from beat times.

        Returns: List of (start_time, duration) tuples
        """
        if not beat_times:
            return []

        # Select subset of beats (not all, to avoid too many movements)
        if len(beat_times) > max_moves:
            # Select evenly spaced beats
            indices = np.linspace(0, len(beat_times) - 1, max_moves, dtype=int)
            selected_beats = [beat_times[i] for i in indices]
        else:
            selected_beats = beat_times

        # Generate movements with some variation in duration
        head_moves = []
        for beat_time in selected_beats:
            duration = np.random.uniform(1.5, 3.0)  # Random duration 1.5-3 seconds
            head_moves.append((beat_time, duration))

        return head_moves

    def analyze_pitch_segments(self, vocals_audio: np.ndarray, sr: float,
                              vocal_segments: List[Tuple[float, float]]) -> List[Tuple[float, float, float, float]]:
        """
        Analyze pitch within vocal segments to create more sophisticated mouth movements.

        Returns: List of (start_time, duration, avg_pitch, pitch_variance) tuples
        """
        pitch_segments = []

        for start_time, duration in vocal_segments:
            # Extract audio segment
            start_sample = int(start_time * sr)
            end_sample = int((start_time + duration) * sr)
            segment_audio = vocals_audio[start_sample:end_sample]

            if len(segment_audio) < sr * 0.1:  # Skip very short segments
                continue

            # Estimate pitch using YIN algorithm
            fmin = librosa.note_to_hz('C2')  # 65 Hz
            fmax = librosa.note_to_hz('C7')  # 2093 Hz
            pitches = librosa.yin(segment_audio, fmin=fmin, fmax=fmax, sr=sr,
                                frame_length=int(sr * 0.05), hop_length=int(sr * 0.025))

            # Filter out unvoiced frames (pitches close to fmax indicate unvoiced)
            voiced_frames = pitches < (fmax * 0.9)  # Consider frames voiced if pitch < 90% of fmax
            pitches = pitches[voiced_frames]
            if len(pitches) == 0:
                continue

            # Calculate pitch statistics
            avg_pitch = np.mean(pitches)
            pitch_variance = np.var(pitches)

            pitch_segments.append((start_time, duration, avg_pitch, pitch_variance))

        return pitch_segments

    def generate_pitch_based_mouth_moves(self, pitch_segments: List[Tuple[float, float, float, float]],
                                       max_moves: int = 50) -> List[Tuple[float, float]]:
        """
        Generate mouth movements based on pitch analysis.

        Returns: List of (start_time, duration) tuples
        """
        if not pitch_segments:
            return []

        mouth_moves = []

        for start_time, duration, avg_pitch, pitch_variance in pitch_segments:
            # Pitch to mouth movement mapping
            # Higher pitch = faster, more precise movements
            # Lower pitch = slower, wider movements
            # High pitch variance = more complex mouth shapes

            # Base duration on pitch (higher pitch = shorter, quicker movements)
            if avg_pitch > librosa.note_to_hz('C5'):  # Above middle C
                base_duration = 0.10  # Quick, precise
                interval = 0.28
            elif avg_pitch > librosa.note_to_hz('C4'):  # Tenor range
                base_duration = 0.15  # Medium
                interval = 0.40
            else:  # Bass range
                base_duration = 0.22  # Slower, wider
                interval = 0.50

            # Adjust for pitch variance (complex melodies need more movements)
            if pitch_variance > 50000:  # Very high variance = complex pitch changes
                interval *= 0.8  # More frequent movements
                base_duration *= 0.95  # Slightly shorter individual movements

            # Generate movements within the segment
            if duration > 0.5:  # Long segments get multiple movements
                num_moves = max(1, int(duration / interval))
                for i in range(num_moves):
                    move_start = start_time + i * interval
                    if move_start < start_time + duration:
                        # Add some variation to duration
                        variation = np.random.uniform(0.7, 1.4)
                        move_duration = base_duration * variation
                        move_duration = round(move_duration, 2)  # Round to 2 decimal places
                        mouth_moves.append((move_start, move_duration))
            else:
                # Short segments get a single movement
                move_duration = round(base_duration * np.random.uniform(0.8, 1.2), 2)
                mouth_moves.append((start_time, move_duration))

        # Sort by time
        mouth_moves.sort(key=lambda x: x[0])

        # Limit the number of mouth moves
        if len(mouth_moves) > max_moves:
            # Select evenly spaced mouth moves
            indices = np.linspace(0, len(mouth_moves) - 1, max_moves, dtype=int)
            mouth_moves = [mouth_moves[i] for i in indices]

        return mouth_moves

    def analyze_song(self, song_dir: str) -> Dict:
        """
        Analyze a song directory and generate automatic movement schedules.

        Returns: Dict with 'head_moves', 'mouth_moves', and updated metadata
        """
        song_path = Path(song_dir)
        results = {
            'head_moves': [],
            'mouth_moves': [],
            'tail_threshold': 1500,  # Default
            'auto_generated': True
        }

        # Load existing metadata for BPM and other settings
        metadata_file = song_path / "metadata.ini"
        bpm = 120.0
        if metadata_file.exists():
            config = configparser.ConfigParser()
            config.read(metadata_file)
            if config.has_section('SONG'):
                bpm = config.getfloat('SONG', 'bpm', fallback=120.0)
                results['tail_threshold'] = config.getfloat('SONG', 'tail_threshold', fallback=1500)

        # Store original BPM for comparison
        original_bpm = bpm

        # Analyze vocals for mouth movements
        vocals_file = song_path / "vocals.wav"
        if vocals_file.exists():
            logger.info(f"Analyzing vocals for mouth movements: {vocals_file}", "🎤")
            vocals_audio, sr = self.load_audio(str(vocals_file))
            vocal_segments = self.detect_vocal_segments(vocals_audio, sr)

            # Analyze pitch within vocal segments for sophisticated mouth movements
            pitch_segments = self.analyze_pitch_segments(vocals_audio, sr, vocal_segments)
            mouth_moves = self.generate_pitch_based_mouth_moves(pitch_segments)

            results['mouth_moves'] = mouth_moves
            logger.info(f"Generated {len(mouth_moves)} pitch-based mouth movements from {len(pitch_segments)} vocal segments", "🗣️")

        # Analyze audio for head movements (prefer full mix, fallback to drums)
        beat_audio_file = song_path / "full.wav"
        if not beat_audio_file.exists():
            beat_audio_file = song_path / "drums.wav"

        if beat_audio_file.exists():
            logger.info(f"Generating head movements based on BPM: {original_bpm}", "🥁")

            # Generate head movements at regular intervals based on BPM
            # Instead of relying on beat detection which may be unreliable
            duration = 40  # Assume 40 second song (will be truncated if shorter)
            beat_length = 60.0 / original_bpm
            head_interval = beat_length * 3  # Head movement every 3 beats

            head_moves = []
            current_time = 2.0  # Start after 2 seconds
            while current_time < duration:
                # Vary duration slightly for natural movement
                duration_variation = np.random.uniform(1.5, 3.0)
                head_moves.append((current_time, duration_variation))
                current_time += head_interval

            # Limit to reasonable number
            if len(head_moves) > 20:
                indices = np.linspace(0, len(head_moves) - 1, 20, dtype=int)
                head_moves = [head_moves[i] for i in indices]

            results['head_moves'] = head_moves
            logger.info(f"Generated {len(head_moves)} head movements at {head_interval:.1f}s intervals", "🤖")

            # Still try to estimate BPM but don't update if it differs significantly
            beat_audio, sr = self.load_audio(str(beat_audio_file))
            beat_times, estimated_bpm = self.detect_beats(beat_audio, sr, original_bpm)

            bpm_change = abs(estimated_bpm - original_bpm) / original_bpm * 100
            if bpm_change > 10:
                logger.warning(f"BPM estimate {estimated_bpm:.1f} differs from current {original_bpm:.1f} ({bpm_change:.0f}% change). Keeping current BPM.", "⚠️")
            else:
                results['bpm'] = round(estimated_bpm, 1)
                logger.info(f"Updated BPM: {original_bpm:.1f} → {estimated_bpm:.1f}", "🎵")

        return results

    def update_song_metadata(self, song_dir: str, analysis_results: Dict) -> bool:
        """
        Update song metadata with automatically generated movement schedules.
        """
        song_path = Path(song_dir)
        metadata_file = song_path / "metadata.ini"

        # Load existing config
        config = configparser.ConfigParser()
        if metadata_file.exists():
            config.read(metadata_file)

        if not config.has_section('SONG'):
            config.add_section('SONG')

        # Convert movement lists to string format
        def format_moves(moves: List[Tuple[float, float]]) -> str:
            return ','.join(f"{time:.1f}:{duration:.1f}" for time, duration in moves)

        config.set('SONG', 'bpm', str(analysis_results.get('bpm', 120.0)))
        config.set('SONG', 'head_moves', format_moves(analysis_results['head_moves']))
        config.set('SONG', 'mouth_moves', format_moves(analysis_results['mouth_moves']))
        config.set('SONG', 'auto_generated_moves', 'true')

        # Save updated metadata
        try:
            with open(metadata_file, 'w') as f:
                config.write(f)
            logger.info(f"Updated metadata for song: {song_path.name}", "💾")
            return True
        except Exception as e:
            logger.error(f"Failed to update metadata: {e}")
            return False


def preprocess_song(song_name: str, song_manager) -> bool:
    """
    Preprocess a single song to generate automatic movements.
    """
    preprocessor = AudioPreprocessor()

    # Get song path
    song_path = song_manager.custom_songs_dir / song_name
    if not song_path.exists():
        song_path = song_manager.example_songs_dir / song_name
        if not song_path.exists():
            logger.error(f"Song not found: {song_name}")
            return False

    logger.info(f"Preprocessing song: {song_name}", "🎵")

    # Analyze the song
    analysis_results = preprocessor.analyze_song(str(song_path))

    # Update metadata
    success = preprocessor.update_song_metadata(str(song_path), analysis_results)

    if success:
        logger.success(f"Successfully preprocessed {song_name}", "✅")
    else:
        logger.error(f"Failed to preprocess {song_name}", "❌")

    return success