# Empty Folder Deleter

Empty Folder Deleter is a simple, user-friendly application designed to help you clean up your file system by identifying and removing empty folders. This tool is perfect for those who want to maintain a tidy directory structure and reclaim disk space.

## Features

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

1. Download the latest `EmptyFolderDeleterSetup.exe` from the [Releases](https://github.com/your-username/empty-folder-deleter/releases) page.
2. Run the installer and follow the on-screen instructions.
3. Once installed, you can run Empty Folder Deleter from the Start menu.

### Option 2: Run from Source

1. Clone this repository:
   ```
   git clone https://github.com/your-username/empty-folder-deleter.git
   ```
2. Navigate to the project directory:
   ```
   cd empty-folder-deleter
   ```
3. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```
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

Contributions to Empty Folder Deleter are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Thanks to all contribution provided by Claude 3.5 Sonnet on helping with error resolution to improve this tool and help convert this app to an actual windows installer.
- Icon provided by [Python libraries].

## Support

If you encounter any problems or have any suggestions, please [open an issue](https://github.com/your-username/empty-folder-deleter/issues) on GitHub.

---

Developed with ❤️ by Jay Singhvi
