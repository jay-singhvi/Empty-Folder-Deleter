# Various Tools

Various Tools is a collection of Windows utilities for tidying up files and media. It is being built as one installable app with a desktop GUI and a command-line interface. The plan is in [docs/MODERNIZATION_PLAN.md](docs/MODERNIZATION_PLAN.md).

This repository currently ships the original **Empty Folder Deleter** app. It is the first tool, and it is being rewritten as part of Various Tools. The description below covers that current app.

## Empty Folder Deleter (current app)

Empty Folder Deleter helps you clean up your file system by identifying and removing empty folders. It is useful for keeping a tidy directory structure and reclaiming disk space.

### Features

- **User-friendly GUI**: Easy-to-use interface for selecting the root directory to scan.
- **Recursive Scanning**: Scans subdirectories to find all empty folders within a given directory.
- **Safe Deletion**: Confirms with the user before deleting any folders.
- **Detailed Logging**: Provides a log of all actions taken, including successful deletions and any errors encountered.
- **Windows Integration**: Option to start the application on Windows startup.

## Installation

### Prerequisites

- Windows operating system
- Python 3.6 or higher (if running from source)

### Option 1: Install from Executable

1. Download the latest `EmptyFolderDeleterSetup.exe` from the [Releases](https://github.com/jay-singhvi/Empty-Folder-Deleter/releases) page.
2. Run the installer and follow the on-screen instructions.
3. Once installed, you can run Empty Folder Deleter from the Start menu.

### Option 2: Run from Source

1. Clone this repository:
   ```
   git clone https://github.com/jay-singhvi/Empty-Folder-Deleter.git
   ```
2. Navigate to the project directory:
   ```
   cd Empty-Folder-Deleter
   ```
3. No third-party packages are needed. The app uses only the Python standard library (Tkinter ships with Python).
4. Run the application:
   ```
   python empty_folder_deleter.py
   ```

## Usage

1. Launch the Empty Folder Deleter application.
2. Click the "Browse" button to select the root directory you want to scan for empty folders.
3. Click the "Start" button to begin the scanning process.
4. Review the list of empty folders found.
5. Confirm the deletion when prompted.
6. Check the log for details on the operation's success.

## Building from Source

To build the executable from source:

1. Install PyInstaller:
   ```
   pip install pyinstaller
   ```
2. Run PyInstaller with the spec file:
   ```
   pyinstaller empty_folder_deleter.spec
   ```
3. The executable will be created in the `dist` folder.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request. Developer guidance (stack, layout, commands and safety rules) is in [AGENTS.md](AGENTS.md).

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Thanks to all contribution provided by Claude 3.5 Sonnet on helping with error resolution to improve this tool and help convert this app to an actual windows installer.
- Icon provided by [Python libraries].

## Support

If you encounter any problems or have any suggestions, please [open an issue](https://github.com/jay-singhvi/Empty-Folder-Deleter/issues) on GitHub.

---

Developed with ❤️ by Jay Singhvi
