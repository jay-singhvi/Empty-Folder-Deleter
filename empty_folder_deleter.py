import os
import logging
import concurrent.futures
import threading
from datetime import datetime
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import sys

# Constants
APP_NAME = "Empty Folder Deleter"
COMPANY_NAME = "Jay Singhvi"
VERSION = "1.0.0"

class DeleteError(Exception):
    pass

class EmptyFolderDeleterGUI:
    def __init__(self, master):
        self.master = master
        master.title(f"{APP_NAME} - {COMPANY_NAME} v{VERSION}")

        # Make the window resizable
        master.resizable(True, True)

        # Configure row and column weights for resizing
        master.grid_rowconfigure(3, weight=1)
        master.grid_columnconfigure(1, weight=1)

        self.path_label = tk.Label(master, text="Root Directory:")
        self.path_label.grid(row=0, column=0, padx=5, pady=5, sticky="w")

        self.path_entry = tk.Entry(master, width=50)
        self.path_entry.grid(row=0, column=1, padx=5, pady=5, sticky="we")

        self.browse_button = tk.Button(
            master, text="Browse", command=self.browse_directory
        )
        self.browse_button.grid(row=0, column=2, padx=5, pady=5)

        self.start_button = tk.Button(
            master, text="Start", command=self.start_search_thread
        )
        self.start_button.grid(row=1, column=1, padx=5, pady=5)

        # Create a frame to hold the text widget and scrollbars
        self.text_frame = tk.Frame(master)
        self.text_frame.grid(
            row=3, column=0, columnspan=3, padx=5, pady=5, sticky="nsew"
        )

        # Configure the text frame for resizing
        self.text_frame.grid_rowconfigure(0, weight=1)
        self.text_frame.grid_columnconfigure(0, weight=1)

        # Create the text widget and scrollbars inside the frame
        self.log_text = tk.Text(
            self.text_frame, height=20, width=100, wrap=tk.NONE, state="disabled"
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.v_scrollbar = tk.Scrollbar(
            self.text_frame, orient="vertical", command=self.log_text.yview
        )
        self.v_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text["yscrollcommand"] = self.v_scrollbar.set

        self.h_scrollbar = tk.Scrollbar(
            self.text_frame, orient="horizontal", command=self.log_text.xview
        )
        self.h_scrollbar.grid(row=1, column=0, sticky="ew")
        self.log_text["xscrollcommand"] = self.h_scrollbar.set

    def browse_directory(self):
        directory = filedialog.askdirectory()
        self.path_entry.delete(0, tk.END)
        self.path_entry.insert(0, directory)

    def start_search_thread(self):
        self.start_button.config(state=tk.DISABLED)
        threading.Thread(target=self.start_search, daemon=True).start()

    def start_search(self):
        root_path = self.path_entry.get()
        if not os.path.isdir(root_path):
            self.show_error("The provided path is not a valid directory.")
            self.start_button.config(state=tk.NORMAL)
            return

        self.clear_log()
        self.recursive_delete_empty_folders(root_path)
        self.start_button.config(state=tk.NORMAL)

    def recursive_delete_empty_folders(self, root_path):
        while True:
            empty_folders = self.find_empty_folders(root_path)
            if not empty_folders:
                self.log_message("No more empty folders found.")
                break

            self.log_message(f"Found {len(empty_folders)} empty folder(s):")
            for folder in empty_folders:
                self.log_message(folder)

            if self.ask_yes_no(f"Delete these {len(empty_folders)} empty folders?"):
                deleted, skipped = self.delete_empty_folders(empty_folders)
                self.log_message(f"\nDeleted {len(deleted)} folders.")
                if skipped:
                    self.log_message(f"Skipped {len(skipped)} folders due to errors.")
            else:
                self.log_message("Operation cancelled. No folders were deleted.")
                break

    def find_empty_folders(self, root_path):
        empty_folders = []
        for dirpath, dirnames, filenames in os.walk(root_path, topdown=False):
            if not dirnames and not filenames:
                empty_folders.append(dirpath)
        return empty_folders

    def delete_empty_folders(self, empty_folders):
        deleted_folders = []
        skipped_folders = []
        for folder in empty_folders:
            try:
                os.rmdir(folder)
                self.log_message(f"Deleted: {folder}")
                deleted_folders.append(folder)
            except OSError as e:
                if e.errno == os.errno.EACCES:
                    self.log_message(f"Permission denied: {folder}")
                elif e.errno == os.errno.ENOTEMPTY:
                    self.log_message(f"Folder not empty: {folder}")
                else:
                    self.log_message(f"Error deleting {folder}: {str(e)}")
                skipped_folders.append(folder)
        return deleted_folders, skipped_folders

    def log_message(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state="disabled")

    def show_error(self, message):
        messagebox.showerror("Error", message)

    def ask_yes_no(self, message):
        return messagebox.askyesno("Confirm Deletion", message)

def main():
    root = tk.Tk()
    EmptyFolderDeleterGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
