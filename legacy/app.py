import streamlit as st
import os
from PIL import Image, ExifTags
from pathlib import Path
import tkinter as tk
from tkinter import filedialog
import subprocess
import time
import pandas as pd

st.title("Image & Video Resolution Reducer")
st.write("Reduce the resolution of images and videos in a folder")

# Helper function to format bytes
def format_bytes(bytes_size):
    """Convert bytes to human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} TB"

# Set up tkinter for folder selection
def select_folder():
    root = tk.Tk()
    root.withdraw()
    root.wm_attributes('-topmost', 1)
    folder_selected = filedialog.askdirectory(master=root)
    root.destroy()
    return folder_selected

# Folder input
col_a, col_b = st.columns([3, 1])
with col_a:
    folder_path = st.text_input("Folder path:", value=st.session_state.get('folder_path', ''))
with col_b:
    st.write("")  # Spacing
    if st.button("Browse..."):
        selected = select_folder()
        if selected:
            st.session_state['folder_path'] = selected
            st.rerun()

if 'folder_path' in st.session_state and st.session_state['folder_path']:
    folder_path = st.session_state['folder_path']

# File type selection
file_type = st.radio(
    "Process:",
    ["Images Only", "Videos Only", "Both Images and Videos"],
    index=0  # Default to "Images Only"
)

# Subdirectory support
include_subdirs = st.checkbox("Include subdirectories", value=False)

# Resolution presets dictionary
RESOLUTION_PRESETS = {
    "360p (640×360)": (640, 360),
    "480p (854×480)": (854, 480),
    "720p HD (1280×720)": (1280, 720),
    "1080p Full HD (1920×1080)": (1920, 1080),
    "2K (2560×1440)": (2560, 1440),
    "4K UHD (3840×2160)": (3840, 2160),
    "Custom": None
}

# Resolution options
col1, col2 = st.columns(2)
with col1:
    resize_method = st.radio(
        "Resize method:",
        ["Resolution Preset", "Percentage", "Custom dimensions"]
    )

with col2:
    if resize_method == "Resolution Preset":
        preset_choice = st.selectbox(
            "Select resolution preset:",
            list(RESOLUTION_PRESETS.keys()),
            index=3  # Default to 1080p Full HD (index 3)
        )
        if preset_choice == "Custom":
            max_width = st.number_input("Max width (pixels):", min_value=100, value=1920)
            max_height = st.number_input("Max height (pixels):", min_value=100, value=1080)
        else:
            max_width, max_height = RESOLUTION_PRESETS[preset_choice]
            st.info(f"Target: {max_width}×{max_height} pixels")
    elif resize_method == "Percentage":
        scale_percentage = st.slider("Scale percentage:", 10, 100, 50)
    else:
        max_width = st.number_input("Max width (pixels):", min_value=100, value=1920)
        max_height = st.number_input("Max height (pixels):", min_value=100, value=1080)

# Output options
output_option = st.radio(
    "Output location:",
    ["Save to new folder", "Overwrite original files"],
    index=0  # Default to "Save to new folder"
)

if output_option == "Save to new folder":
    output_folder = st.text_input("Output folder name:", "resized_images")

# Quality setting
col_q1, col_q2 = st.columns(2)
with col_q1:
    if file_type in ["Images Only", "Both Images and Videos"]:
        quality = st.slider(
            "JPEG quality (1-100):",
            1, 100, 85,
            help="85-90: Excellent quality, good compression (recommended)\n90-95: Near-original quality\n95-100: Maximum quality, minimal compression"
        )
with col_q2:
    if file_type in ["Videos Only", "Both Images and Videos"]:
        video_quality = st.selectbox(
            "Video quality preset:",
            ["High (CRF 18)", "Medium (CRF 23)", "Low (CRF 28)"],
            index=0  # Default to High
        )
        # Extract CRF value
        crf_value = int(video_quality.split("CRF ")[1].split(")")[0])

# Dry run mode
dry_run = st.checkbox("🔍 Dry run (preview only, don't process)", value=False)

# Process button
if st.button("Process Files" if not dry_run else "Preview Changes"):
    if not folder_path or not os.path.exists(folder_path):
        st.error("Please enter a valid folder path")
    else:
        # Supported formats
        image_formats = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tiff', '.webp'}
        video_formats = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm', '.m4v'}

        # Statistics tracking
        stats = {
            'total_found': 0,
            'processed': 0,
            'skipped': [],
            'errors': [],
            'original_size': 0,
            'final_size': 0,
            'start_time': time.time(),
            'comparison_data': []  # For batch comparison view
        }

        # Get files based on selection
        all_files = []

        if include_subdirs:
            # Use pathlib for recursive search
            folder_pathlib = Path(folder_path)
            if file_type in ["Images Only", "Both Images and Videos"]:
                for ext in image_formats:
                    all_files.extend(folder_pathlib.rglob(f'*{ext}'))
                    all_files.extend(folder_pathlib.rglob(f'*{ext.upper()}'))
            if file_type in ["Videos Only", "Both Images and Videos"]:
                for ext in video_formats:
                    all_files.extend(folder_pathlib.rglob(f'*{ext}'))
                    all_files.extend(folder_pathlib.rglob(f'*{ext.upper()}'))
            # Remove duplicates and convert to list of tuples (relative_path, full_path)
            all_files = list(set(all_files))
            file_tuples = [(str(f.relative_to(folder_pathlib)), str(f)) for f in all_files]
        else:
            # Non-recursive search
            files_in_folder = os.listdir(folder_path)
            if file_type in ["Images Only", "Both Images and Videos"]:
                all_files.extend([f for f in files_in_folder if Path(f).suffix.lower() in image_formats])
            if file_type in ["Videos Only", "Both Images and Videos"]:
                all_files.extend([f for f in files_in_folder if Path(f).suffix.lower() in video_formats])
            file_tuples = [(f, os.path.join(folder_path, f)) for f in all_files]

        # Separate into image and video files
        image_file_tuples = [(rel, full) for rel, full in file_tuples if Path(full).suffix.lower() in image_formats]
        video_file_tuples = [(rel, full) for rel, full in file_tuples if Path(full).suffix.lower() in video_formats]

        stats['total_found'] = len(file_tuples)

        if stats['total_found'] == 0:
            st.warning(f"No {file_type.lower()} found in the specified folder" + (" and subdirectories" if include_subdirs else ""))
        else:
            # Preview message
            st.info(f"📊 Found {len(image_file_tuples)} images and {len(video_file_tuples)} videos")

            # Create output folder if needed (not in dry run)
            if output_option == "Save to new folder" and not dry_run:
                output_path = os.path.join(folder_path, output_folder)
                os.makedirs(output_path, exist_ok=True)
            elif output_option == "Save to new folder":
                output_path = os.path.join(folder_path, output_folder)
            else:
                output_path = folder_path

            # Progress bar
            progress_bar = st.progress(0)
            status_text = st.empty()

            current_file = 0

            # Process images
            for rel_path, img_path in image_file_tuples:
                try:
                    # Update status
                    display_name = rel_path if include_subdirs else os.path.basename(img_path)
                    status_text.text(f"{'Analyzing' if dry_run else 'Processing'}: {display_name}")

                    # Get original file size
                    original_size = os.path.getsize(img_path)

                    # Open image
                    img = Image.open(img_path)

                    # Preserve EXIF data and handle orientation
                    exif_data = None
                    try:
                        exif_data = img.getexif()
                        # Apply EXIF orientation to the image
                        if exif_data is not None:
                            orientation_key = None
                            for key, val in ExifTags.TAGS.items():
                                if val == 'Orientation':
                                    orientation_key = key
                                    break

                            if orientation_key and orientation_key in exif_data:
                                orientation = exif_data[orientation_key]

                                # Apply rotation based on EXIF orientation
                                if orientation == 3:
                                    img = img.rotate(180, expand=True)
                                elif orientation == 6:
                                    img = img.rotate(270, expand=True)
                                elif orientation == 8:
                                    img = img.rotate(90, expand=True)

                                # Reset orientation to normal after rotation
                                exif_data[orientation_key] = 1
                    except (AttributeError, KeyError, IndexError):
                        # If EXIF data doesn't exist or is corrupted, continue without it
                        pass

                    # Calculate new dimensions
                    if resize_method == "Percentage":
                        new_width = int(img.width * scale_percentage / 100)
                        new_height = int(img.height * scale_percentage / 100)
                        will_process = True
                        skip_reason = None
                    else:  # Resolution Preset or Custom dimensions
                        # CRITICAL FIX: Only process if BOTH dimensions are greater than target
                        # This ensures we only reduce images that are larger in both width AND height
                        if img.width > max_width and img.height > max_height:
                            # Maintain aspect ratio
                            aspect_ratio = img.width / img.height

                            # Calculate scale factor based on the limiting dimension
                            scale_width = max_width / img.width
                            scale_height = max_height / img.height

                            # Use the SMALLER scale factor to ensure output fits WITHIN target resolution
                            # This means at least one dimension will match the target, and the other will be equal or smaller
                            scale_factor = min(scale_width, scale_height)

                            new_width = int(img.width * scale_factor)
                            new_height = int(img.height * scale_factor)
                            will_process = True
                            skip_reason = None
                        else:
                            # Skip this file - doesn't meet the criteria
                            new_width = img.width
                            new_height = img.height
                            will_process = False

                            # Determine skip reason
                            if img.width <= max_width and img.height <= max_height:
                                skip_reason = "Already smaller than target resolution"
                            else:
                                skip_reason = "Only one dimension exceeds target (needs both)"

                    # Track statistics
                    if not will_process:
                        stats['skipped'].append({
                            'filename': display_name,
                            'reason': skip_reason,
                            'original_size': format_bytes(original_size),
                            'original_dims': f"{img.width}×{img.height}"
                        })
                        stats['comparison_data'].append({
                            'filename': display_name,
                            'original_size': format_bytes(original_size),
                            'new_size': format_bytes(original_size),
                            'savings': '0%',
                            'original_dims': f"{img.width}×{img.height}",
                            'new_dims': f"{img.width}×{img.height}",
                            'status': f"⊘ Skipped - {skip_reason}"
                        })
                    else:
                        if not dry_run:
                            # Only resize if dimensions changed
                            if new_width != img.width or new_height != img.height:
                                resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                            else:
                                resized_img = img

                            # Save image
                            if include_subdirs:
                                # Preserve folder structure
                                output_file = os.path.join(output_path, rel_path)
                                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                            else:
                                output_file = os.path.join(output_path, os.path.basename(img_path))

                            # Determine save parameters
                            save_kwargs = {}
                            if Path(img_path).suffix.lower() in {'.jpg', '.jpeg'}:
                                save_kwargs['quality'] = quality
                                save_kwargs['optimize'] = True
                                # Save EXIF data if available
                                if exif_data is not None and len(exif_data) > 0:
                                    save_kwargs['exif'] = exif_data
                            elif Path(img_path).suffix.lower() == '.png':
                                save_kwargs['optimize'] = True

                            resized_img.save(output_file, **save_kwargs)

                            # Get final file size
                            final_size = os.path.getsize(output_file)
                            stats['original_size'] += original_size
                            stats['final_size'] += final_size
                            stats['processed'] += 1

                            # Calculate savings
                            savings_pct = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0
                            stats['comparison_data'].append({
                                'filename': display_name,
                                'original_size': format_bytes(original_size),
                                'new_size': format_bytes(final_size),
                                'savings': f"{savings_pct:.1f}%",
                                'original_dims': f"{img.width}×{img.height}",
                                'new_dims': f"{new_width}×{new_height}",
                                'status': "✓ Processed"
                            })
                        else:
                            # Dry run: estimate file size (rough approximation)
                            estimated_size = int(original_size * (new_width * new_height) / (img.width * img.height))
                            if Path(img_path).suffix.lower() in {'.jpg', '.jpeg'}:
                                estimated_size = int(estimated_size * (quality / 100))

                            stats['original_size'] += original_size
                            stats['final_size'] += estimated_size
                            stats['processed'] += 1

                            savings_pct = ((original_size - estimated_size) / original_size * 100) if original_size > 0 else 0
                            stats['comparison_data'].append({
                                'filename': display_name,
                                'original_size': format_bytes(original_size),
                                'new_size': format_bytes(estimated_size) + " (est.)",
                                'savings': f"{savings_pct:.1f}%",
                                'original_dims': f"{img.width}×{img.height}",
                                'new_dims': f"{new_width}×{new_height}",
                                'status': "✓ Will process"
                            })

                except Exception as e:
                    stats['errors'].append({
                        'filename': display_name,
                        'error_message': str(e)
                    })
                    stats['comparison_data'].append({
                        'filename': display_name,
                        'original_size': 'N/A',
                        'new_size': 'N/A',
                        'savings': 'N/A',
                        'original_dims': 'N/A',
                        'new_dims': 'N/A',
                        'status': f"✗ Error - {str(e)[:50]}"
                    })

                # Update progress
                current_file += 1
                progress_bar.progress(current_file / stats['total_found'])

            # Process videos
            for rel_path, video_path in video_file_tuples:
                try:
                    # Update status
                    display_name = rel_path if include_subdirs else os.path.basename(video_path)
                    status_text.text(f"{'Analyzing' if dry_run else 'Processing'} video: {display_name}")

                    # Check if FFmpeg is available (only once)
                    if video_file_tuples.index((rel_path, video_path)) == 0:
                        try:
                            check_ffmpeg = subprocess.run(['ffmpeg', '-version'],
                                                         capture_output=True,
                                                         text=True,
                                                         timeout=5)
                            if check_ffmpeg.returncode != 0:
                                st.error("⚠️ FFmpeg is not installed or not in PATH. Cannot process videos.")
                                st.info("📥 Install FFmpeg from https://ffmpeg.org/download.html or use 'winget install FFmpeg' on Windows")
                                for remaining_rel, remaining_full in video_file_tuples:
                                    stats['errors'].append({
                                        'filename': remaining_rel if include_subdirs else os.path.basename(remaining_full),
                                        'error_message': 'FFmpeg not available'
                                    })
                                break
                        except FileNotFoundError:
                            st.error("⚠️ FFmpeg is not installed or not in PATH. Cannot process videos.")
                            st.info("📥 Install FFmpeg from https://ffmpeg.org/download.html or use 'winget install FFmpeg' on Windows")
                            for remaining_rel, remaining_full in video_file_tuples:
                                stats['errors'].append({
                                    'filename': remaining_rel if include_subdirs else os.path.basename(remaining_full),
                                    'error_message': 'FFmpeg not available'
                                })
                            break
                        except Exception as e:
                            st.error(f"❌ Error checking FFmpeg: {str(e)}")
                            for remaining_rel, remaining_full in video_file_tuples:
                                stats['errors'].append({
                                    'filename': remaining_rel if include_subdirs else os.path.basename(remaining_full),
                                    'error_message': f'FFmpeg check failed: {str(e)}'
                                })
                            break

                    # Get original file size
                    original_size = os.path.getsize(video_path)

                    # Get video dimensions (these are the actual encoded dimensions)
                    probe_cmd = [
                        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream=width,height',
                        '-of', 'csv=p=0', video_path
                    ]
                    result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)

                    if result.returncode != 0:
                        stats['errors'].append({
                            'filename': display_name,
                            'error_message': 'Could not read video dimensions'
                        })
                        stats['comparison_data'].append({
                            'filename': display_name,
                            'original_size': format_bytes(original_size),
                            'new_size': 'N/A',
                            'savings': 'N/A',
                            'original_dims': 'N/A',
                            'new_dims': 'N/A',
                            'status': "✗ Error - Cannot read dimensions"
                        })
                        current_file += 1
                        progress_bar.progress(current_file / stats['total_found'])
                        continue

                    # Parse video dimensions
                    output_parts = result.stdout.strip().split(',')
                    orig_width = int(output_parts[0])
                    orig_height = int(output_parts[1])

                    # Check for rotation metadata (display rotation)
                    rotation_probe_cmd = [
                        'ffprobe', '-v', 'error', '-select_streams', 'v:0',
                        '-show_entries', 'stream_tags=rotate:stream_side_data=rotation',
                        '-of', 'default=nw=1:nk=1', video_path
                    ]
                    rotation_result = subprocess.run(rotation_probe_cmd, capture_output=True, text=True, timeout=10)
                    rotation = 0
                    if rotation_result.returncode == 0 and rotation_result.stdout.strip():
                        try:
                            # Parse rotation from output
                            for line in rotation_result.stdout.strip().split('\n'):
                                if line.strip() and line.strip() != '0':
                                    rotation = int(float(line.strip()))
                                    break
                        except:
                            rotation = 0

                    # For display dimensions (what user sees), swap if rotated 90/270
                    display_width = orig_width
                    display_height = orig_height
                    if abs(rotation) == 90 or abs(rotation) == 270:
                        display_width, display_height = orig_height, orig_width

                    # Calculate target resolution based on resize method
                    # Use display dimensions for calculation (what user sees)
                    if resize_method == "Percentage":
                        target_display_width = int(display_width * scale_percentage / 100)
                        target_display_height = int(display_height * scale_percentage / 100)
                        will_process = True
                        skip_reason = None
                    else:
                        # CRITICAL FIX: Only process if BOTH dimensions are greater than target
                        # This ensures we only reduce videos that are larger in both width AND height
                        if display_width > max_width and display_height > max_height:
                            # Calculate scale factor based on the limiting dimension
                            scale_width = max_width / display_width
                            scale_height = max_height / display_height

                            # Use the SMALLER scale factor to ensure output fits WITHIN target resolution
                            # This means at least one dimension will match the target, and the other will be equal or smaller
                            scale_factor = min(scale_width, scale_height)

                            # Apply the scale factor
                            target_display_width = int(display_width * scale_factor)
                            target_display_height = int(display_height * scale_factor)
                            will_process = True
                            skip_reason = None
                        else:
                            # Skip this video - doesn't meet the criteria
                            target_display_width = display_width
                            target_display_height = display_height
                            will_process = False

                            # Determine skip reason
                            if display_width <= max_width and display_height <= max_height:
                                skip_reason = "Already smaller than target resolution"
                            else:
                                skip_reason = "Only one dimension exceeds target (needs both)"

                    # Track statistics
                    if not will_process:
                        stats['skipped'].append({
                            'filename': display_name,
                            'reason': skip_reason,
                            'original_size': format_bytes(original_size),
                            'original_dims': f"{display_width}×{display_height}"
                        })
                        stats['comparison_data'].append({
                            'filename': display_name,
                            'original_size': format_bytes(original_size),
                            'new_size': format_bytes(original_size),
                            'savings': '0%',
                            'original_dims': f"{display_width}×{display_height}",
                            'new_dims': f"{display_width}×{display_height}",
                            'status': f"⊘ Skipped - {skip_reason}"
                        })
                    else:
                        # Convert display dimensions back to encoded dimensions
                        # If video has 90/270 rotation, swap back for encoding
                        if abs(rotation) == 90 or abs(rotation) == 270:
                            target_width = target_display_height
                            target_height = target_display_width
                        else:
                            target_width = target_display_width
                            target_height = target_display_height

                        # Ensure even dimensions (required by most codecs)
                        target_width = target_width if target_width % 2 == 0 else target_width - 1
                        target_height = target_height if target_height % 2 == 0 else target_height - 1

                        if not dry_run:
                            # Save video
                            if include_subdirs:
                                # Preserve folder structure
                                output_file = os.path.join(output_path, rel_path)
                                os.makedirs(os.path.dirname(output_file), exist_ok=True)
                            else:
                                output_file = os.path.join(output_path, os.path.basename(video_path))

                            # FFmpeg command to resize video WITHOUT changing orientation
                            video_filters = f"scale={target_width}:{target_height}"

                            ffmpeg_cmd = [
                                'ffmpeg', '-i', video_path,
                                '-vf', video_filters,
                                '-c:v', 'libx264',
                                '-crf', str(crf_value),
                                '-preset', 'medium',
                                '-c:a', 'copy',  # Copy audio without re-encoding
                                '-map_metadata', '0',  # Copy all metadata from input
                                '-movflags', 'use_metadata_tags',  # Preserve metadata tags
                                '-y',  # Overwrite output file
                                output_file
                            ]

                            # Run FFmpeg
                            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)

                            if result.returncode == 0:
                                final_size = os.path.getsize(output_file)
                                stats['original_size'] += original_size
                                stats['final_size'] += final_size
                                stats['processed'] += 1

                                savings_pct = ((original_size - final_size) / original_size * 100) if original_size > 0 else 0
                                stats['comparison_data'].append({
                                    'filename': display_name,
                                    'original_size': format_bytes(original_size),
                                    'new_size': format_bytes(final_size),
                                    'savings': f"{savings_pct:.1f}%",
                                    'original_dims': f"{display_width}×{display_height}",
                                    'new_dims': f"{target_display_width}×{target_display_height}",
                                    'status': "✓ Processed"
                                })
                            else:
                                error_msg = result.stderr[:200] if result.stderr else "Unknown FFmpeg error"
                                stats['errors'].append({
                                    'filename': display_name,
                                    'error_message': error_msg
                                })
                                stats['comparison_data'].append({
                                    'filename': display_name,
                                    'original_size': format_bytes(original_size),
                                    'new_size': 'N/A',
                                    'savings': 'N/A',
                                    'original_dims': f"{display_width}×{display_height}",
                                    'new_dims': 'N/A',
                                    'status': f"✗ FFmpeg Error"
                                })
                        else:
                            # Dry run: estimate file size (very rough approximation)
                            pixel_reduction = (target_display_width * target_display_height) / (display_width * display_height)
                            estimated_size = int(original_size * pixel_reduction * 0.8)  # 0.8 factor for compression

                            stats['original_size'] += original_size
                            stats['final_size'] += estimated_size
                            stats['processed'] += 1

                            savings_pct = ((original_size - estimated_size) / original_size * 100) if original_size > 0 else 0
                            stats['comparison_data'].append({
                                'filename': display_name,
                                'original_size': format_bytes(original_size),
                                'new_size': format_bytes(estimated_size) + " (est.)",
                                'savings': f"{savings_pct:.1f}%",
                                'original_dims': f"{display_width}×{display_height}",
                                'new_dims': f"{target_display_width}×{target_display_height}",
                                'status': "✓ Will process"
                            })

                except Exception as e:
                    stats['errors'].append({
                        'filename': display_name,
                        'error_message': str(e)
                    })
                    stats['comparison_data'].append({
                        'filename': display_name,
                        'original_size': 'N/A',
                        'new_size': 'N/A',
                        'savings': 'N/A',
                        'original_dims': 'N/A',
                        'new_dims': 'N/A',
                        'status': f"✗ Error - {str(e)[:50]}"
                    })

                # Update progress
                current_file += 1
                progress_bar.progress(current_file / stats['total_found'])

            # Clear status and progress
            status_text.empty()
            progress_bar.empty()

            # Calculate processing time
            elapsed = time.time() - stats['start_time']
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)

            # Display comprehensive summary
            st.markdown("---")
            if dry_run:
                st.success(f"🔍 **Dry Run Complete!**")
            else:
                st.success(f"✅ **Processing Complete!**")

            st.markdown("### 📊 Summary")

            col_s1, col_s2, col_s3 = st.columns(3)
            with col_s1:
                st.metric("Total Files Found", stats['total_found'])
            with col_s2:
                st.metric("Processed" if not dry_run else "Will Process", stats['processed'])
            with col_s3:
                st.metric("Skipped", len(stats['skipped']))

            # Images vs Videos breakdown
            processed_images = sum(1 for item in stats['comparison_data'] if item['status'].startswith('✓') and any(item['filename'].lower().endswith(ext) for ext in image_formats))
            processed_videos = sum(1 for item in stats['comparison_data'] if item['status'].startswith('✓') and any(item['filename'].lower().endswith(ext) for ext in video_formats))

            if processed_images > 0 or processed_videos > 0:
                st.write(f"- **Images:** {processed_images} {'processed' if not dry_run else 'will be processed'}")
                st.write(f"- **Videos:** {processed_videos} {'processed' if not dry_run else 'will be processed'}")

            # Storage savings
            if stats['original_size'] > 0:
                st.markdown("### 💾 Storage Savings")
                col_st1, col_st2, col_st3 = st.columns(3)
                with col_st1:
                    st.metric("Original Total", format_bytes(stats['original_size']))
                with col_st2:
                    st.metric("Final Total" + (" (est.)" if dry_run else ""), format_bytes(stats['final_size']))
                with col_st3:
                    savings_pct = ((stats['original_size'] - stats['final_size']) / stats['original_size'] * 100) if stats['original_size'] > 0 else 0
                    st.metric("Space Saved", f"{format_bytes(stats['original_size'] - stats['final_size'])} ({savings_pct:.1f}%)")

            # Processing time
            st.markdown(f"### ⏱️ Processing Time: {minutes}m {seconds}s")

            # Skipped files details
            if len(stats['skipped']) > 0:
                with st.expander(f"📋 Skipped Files Details ({len(stats['skipped'])} files)"):
                    for skip in stats['skipped']:
                        st.write(f"**{skip['filename']}**")
                        st.write(f"  - Reason: {skip['reason']}")
                        st.write(f"  - Original: {skip['original_dims']} ({skip['original_size']})")
                        st.write("")

            # Error details
            if len(stats['errors']) > 0:
                with st.expander(f"❌ Errors ({len(stats['errors'])} files)", expanded=True):
                    for error in stats['errors']:
                        st.error(f"**{error['filename']}**: {error['error_message']}")

            # Batch comparison view
            if len(stats['comparison_data']) > 0:
                with st.expander("📊 File-by-File Comparison (click to expand)"):
                    # Convert comparison data to DataFrame
                    df = pd.DataFrame(stats['comparison_data'])

                    # Rename columns for better display
                    df.columns = ['Filename', 'Original Size', 'New Size', 'Savings', 'Original Dims', 'New Dims', 'Status']

                    # Display as Streamlit dataframe with full width
                    st.dataframe(df, use_container_width=True, hide_index=True)

            # Output location info
            if output_option == "Save to new folder":
                if dry_run:
                    st.info(f"📁 Files will be saved to: {output_path}")
                else:
                    st.info(f"📁 Processed files saved to: {output_path}")

            # Dry run call to action
            if dry_run and stats['processed'] > 0:
                st.markdown("---")
                st.info("✨ Ready to proceed? Uncheck 'Dry run' and click 'Process Files' to apply these changes.")

# Display info
st.sidebar.header("ℹ️ Information")
st.sidebar.write("""
**Supported Image Formats:**
- JPEG (.jpg, .jpeg)
- PNG (.png)
- BMP (.bmp)
- GIF (.gif)
- TIFF (.tiff)
- WebP (.webp)

**Supported Video Formats:**
- MP4 (.mp4)
- AVI (.avi)
- MOV (.mov)
- MKV (.mkv)
- WMV (.wmv)
- FLV (.flv)
- WebM (.webm)
- M4V (.m4v)

**Important: Processing Logic**
- Files are ONLY processed if BOTH width AND height are greater than the target resolution
- Example: For 1920×1080 target, a 2560×800 video will be SKIPPED (800 < 1080)
- Files will NEVER be upscaled, only reduced
- Original aspect ratio and orientation are always preserved

**Tips:**
- Resolution presets: Choose standard resolutions like 480p, 720p, 1080p, etc.
- Percentage scaling for uniform reduction
- Use dry run mode to preview changes before processing
- For images: EXIF metadata is preserved
- For videos: Requires FFmpeg to be installed and in PATH
- Video CRF: Lower values = higher quality (18=high, 23=medium, 28=low)
- Quality 85-90: Best balance of quality and file size (recommended)
""")
