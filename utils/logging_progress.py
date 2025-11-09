"""
Thread-safe logging and progress management infrastructure.

This module provides UI logging handlers and progress tracking for
long-running operations across all tabs.
"""

import logging
import os
from queue import Queue, Empty
from typing import Dict, Callable, Optional, List
from datetime import datetime
from PyQt5.QtCore import QObject, pyqtSignal, QTimer
from PyQt5.QtWidgets import QTextEdit


class UILogHandler(logging.Handler):
    """
    Thread-safe log handler that queues messages for UI display.
    
    This handler can be called from any thread but only updates
    the UI widget from the main thread via a QTimer.
    """
    
    def __init__(self, log_widget: QTextEdit):
        """
        Initialize UI log handler.
        
        Args:
            log_widget: QTextEdit widget to display logs
        """
        super().__init__()
        self.log_widget = log_widget
        self.queue = Queue()
        
        # Timer to process queue in main thread
        self.timer = QTimer()
        self.timer.timeout.connect(self._process_queue)
        self.timer.start(100)  # Process every 100ms
        
        # Set formatter
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        )
        self.setFormatter(formatter)
    
    def emit(self, record):
        """
        Queue a log record (called from any thread).
        
        Args:
            record: LogRecord to emit
        """
        try:
            msg = self.format(record)
            self.queue.put((record.levelname, msg))
        except Exception:
            self.handleError(record)
    
    def _process_queue(self):
        """Process queued log messages (main thread only)"""
        count = 0
        max_per_cycle = 10  # Process max 10 messages per timer tick
        
        while count < max_per_cycle:
            try:
                level, msg = self.queue.get_nowait()
                self._append_to_widget(level, msg)
                count += 1
            except Empty:
                break
    
    def _append_to_widget(self, level: str, msg: str):
        """
        Append formatted message to widget.
        
        Args:
            level: Log level (INFO, WARNING, ERROR, etc.)
            msg: Formatted message
        """
        # Color-code by level
        color_map = {
            'DEBUG': '#888888',
            'INFO': '#000000',
            'WARNING': '#ff8800',
            'ERROR': '#ff0000',
            'CRITICAL': '#cc0000'
        }
        
        color = color_map.get(level, '#000000')
        html_msg = f'<span style="color: {color};">{msg}</span>'
        
        self.log_widget.append(html_msg)
        
        # Auto-scroll to bottom
        scrollbar = self.log_widget.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
    
    def stop(self):
        """Stop the timer and clean up"""
        self.timer.stop()


class FileLogHandler(logging.FileHandler):
    """
    File handler for persistent session logs.
    
    Saves logs to project directory for post-mortem analysis.
    """
    
    def __init__(self, log_file_path: str):
        """
        Initialize file log handler.
        
        Args:
            log_file_path: Path to log file
        """
        # Ensure directory exists
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        
        super().__init__(log_file_path, mode='a', encoding='utf-8')
        
        # Set detailed formatter for file
        formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.setFormatter(formatter)


class ProgressTask:
    """
    Represents a single progress task.
    
    Tracks current progress, total items, status, and description.
    """
    
    def __init__(self, task_id: str, total: int, description: str):
        """
        Initialize progress task.
        
        Args:
            task_id: Unique task identifier
            total: Total number of items
            description: Human-readable description
        """
        self.task_id = task_id
        self.total = total
        self.current = 0
        self.description = description
        self.status = "in_progress"  # "in_progress" | "completed" | "failed" | "cancelled"
        self.error_message: Optional[str] = None
        self.started_at = datetime.now()
        self.completed_at: Optional[datetime] = None
    
    def get_percentage(self) -> int:
        """Get completion percentage"""
        if self.total == 0:
            return 100 if self.status == "completed" else 0
        return int((self.current / self.total) * 100)
    
    def get_elapsed_seconds(self) -> float:
        """Get elapsed time in seconds"""
        end_time = self.completed_at or datetime.now()
        return (end_time - self.started_at).total_seconds()


class ProgressManager(QObject):
    """
    Manage multiple concurrent progress tasks.
    
    Provides thread-safe progress tracking and notifications.
    
    Signals:
        progress_updated: Emitted when any task progress changes (task_id, current, total, percentage)
        task_completed: Emitted when task completes (task_id, success)
        task_failed: Emitted when task fails (task_id, error_message)
    """
    
    progress_updated = pyqtSignal(str, int, int, int)  # task_id, current, total, percentage
    task_completed = pyqtSignal(str, bool)  # task_id, success
    task_failed = pyqtSignal(str, str)  # task_id, error_message
    
    def __init__(self):
        super().__init__()
        self.tasks: Dict[str, ProgressTask] = {}
        self.listeners: List[Callable] = []
    
    def create_task(self, task_id: str, total: int, description: str) -> ProgressTask:
        """
        Create a new progress task.
        
        Args:
            task_id: Unique task identifier
            total: Total number of items
            description: Human-readable description
            
        Returns:
            Created ProgressTask
        """
        task = ProgressTask(task_id, total, description)
        self.tasks[task_id] = task
        self._notify_update(task)
        return task
    
    def update_task(self, task_id: str, current: int, status: Optional[str] = None):
        """
        Update task progress (thread-safe).
        
        Args:
            task_id: Task identifier
            current: Current progress value
            status: Optional status update
        """
        if task_id not in self.tasks:
            return
        
        task = self.tasks[task_id]
        task.current = min(current, task.total)  # Don't exceed total
        
        if status:
            task.status = status
        
        self._notify_update(task)
    
    def increment_task(self, task_id: str, amount: int = 1):
        """
        Increment task progress by amount.
        
        Args:
            task_id: Task identifier
            amount: Amount to increment by
        """
        if task_id not in self.tasks:
            return
        
        task = self.tasks[task_id]
        task.current = min(task.current + amount, task.total)
        self._notify_update(task)
    
    def complete_task(self, task_id: str, success: bool = True, error_message: Optional[str] = None):
        """
        Mark task as completed.
        
        Args:
            task_id: Task identifier
            success: Whether task completed successfully
            error_message: Optional error message if failed
        """
        if task_id not in self.tasks:
            return
        
        task = self.tasks[task_id]
        task.status = "completed" if success else "failed"
        task.completed_at = datetime.now()
        task.error_message = error_message
        
        if success:
            task.current = task.total  # Ensure 100%
            self.task_completed.emit(task_id, True)
        else:
            self.task_failed.emit(task_id, error_message or "Unknown error")
        
        self._notify_update(task)
    
    def cancel_task(self, task_id: str):
        """
        Cancel a task.
        
        Args:
            task_id: Task identifier
        """
        if task_id not in self.tasks:
            return
        
        task = self.tasks[task_id]
        task.status = "cancelled"
        task.completed_at = datetime.now()
        self._notify_update(task)
    
    def get_task(self, task_id: str) -> Optional[ProgressTask]:
        """
        Get task by ID.
        
        Args:
            task_id: Task identifier
            
        Returns:
            ProgressTask or None
        """
        return self.tasks.get(task_id)
    
    def get_all_tasks(self) -> List[ProgressTask]:
        """Get all tasks"""
        return list(self.tasks.values())
    
    def clear_completed_tasks(self):
        """Remove completed tasks from tracking"""
        self.tasks = {
            tid: task for tid, task in self.tasks.items()
            if task.status not in ("completed", "failed", "cancelled")
        }
    
    def _notify_update(self, task: ProgressTask):
        """
        Notify listeners of task update.
        
        Args:
            task: Updated task
        """
        self.progress_updated.emit(
            task.task_id,
            task.current,
            task.total,
            task.get_percentage()
        )
        
        # Call registered listeners
        for listener in self.listeners:
            try:
                listener()
            except Exception as e:
                print(f"Error in progress listener: {e}")
    
    def add_listener(self, callback: Callable):
        """
        Add a progress update listener.
        
        Args:
            callback: Function to call on updates
        """
        if callback not in self.listeners:
            self.listeners.append(callback)
    
    def remove_listener(self, callback: Callable):
        """
        Remove a progress update listener.
        
        Args:
            callback: Function to remove
        """
        if callback in self.listeners:
            self.listeners.remove(callback)


def setup_logger(
    name: str,
    log_widget: Optional[QTextEdit] = None,
    log_file: Optional[str] = None,
    level: int = logging.INFO
) -> logging.Logger:
    """
    Setup a logger with UI and/or file handlers.
    
    Args:
        name: Logger name
        log_widget: Optional QTextEdit for UI logging
        log_file: Optional file path for persistent logging
        level: Logging level
        
    Returns:
        Configured logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Add UI handler if widget provided
    if log_widget is not None:
        ui_handler = UILogHandler(log_widget)
        ui_handler.setLevel(level)
        logger.addHandler(ui_handler)
    
    # Add file handler if path provided
    if log_file is not None:
        file_handler = FileLogHandler(log_file)
        file_handler.setLevel(logging.DEBUG)  # Always log everything to file
        logger.addHandler(file_handler)
    
    # Prevent propagation to root logger
    logger.propagate = False
    
    return logger


class LogContext:
    """
    Context manager for temporary log level changes.
    
    Usage:
        with LogContext(logger, logging.DEBUG):
            # Detailed logging here
            pass
    """
    
    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.new_level = level
        self.old_level = logger.level
    
    def __enter__(self):
        self.logger.setLevel(self.new_level)
        return self.logger
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.logger.setLevel(self.old_level)
