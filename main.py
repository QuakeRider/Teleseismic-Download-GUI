"""
Seismic Data Downloader - Main Entry Point

Standalone application for event/station selection and waveform downloading.
"""

import sys
import argparse
import logging
from pathlib import Path
from PyQt5.QtCore import Qt, QCoreApplication
from PyQt5.QtWidgets import QApplication, QDialog

# Import data manager
from data.data_manager import DataManager

# GUI components are imported inside main() after QApplication is created


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Seismic Data Downloader - Event and Station Selector with Waveform Download'
    )
    
    parser.add_argument(
        '--project',
        type=str,
        default=None,
        help='Project directory path'
    )
    
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug logging'
    )
    
    return parser.parse_args()


def setup_logging(debug=False):
    """Setup console logging."""
    level = logging.DEBUG if debug else logging.INFO
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    return logging.getLogger('seismic_downloader')


def main():
    """Main entry point."""
    # Parse arguments
    args = parse_arguments()
    
    # Setup logging
    logger = setup_logging(debug=args.debug)
    logger.info("Starting Seismic Data Downloader...")
    
    # Initialize data manager
    data_manager = DataManager()
    
    # Set project directory if provided
    if args.project:
        project_path = Path(args.project)
        if project_path.exists():
            data_manager.project_dir = project_path
            logger.info(f"Using project directory: {project_path}")
        else:
            logger.info(f"Project directory does not exist, will create: {project_path}")
            data_manager.initialize_project(str(project_path))
    
    # Set Qt attributes BEFORE creating the application (required by QtWebEngine)
    QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QCoreApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

    # Optionally import QtWebEngineWidgets before creating the app (satisfies plugin init)
    try:
        from PyQt5 import QtWebEngineWidgets  # noqa: F401
    except Exception:
        pass

    # Create Qt application
    app = QApplication(sys.argv)
    app.setApplicationName("Seismic Data Downloader")
    app.setOrganizationName("SeismicTools")
    
    # Set application style
    app.setStyle('Fusion')
    
    # Import GUI components after QApplication is created
    from gui.main_window import MainWindow, ModeSelectionDialog

    # Prompt user to select mode at startup
    mode_dialog = ModeSelectionDialog()
    if mode_dialog.exec_() != QDialog.Accepted:
        logger.info("Mode selection cancelled. Exiting.")
        return 0
    mode = mode_dialog.selected_mode()

    main_window = MainWindow(data_manager, logger, mode=mode)
    main_window.show()
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
