from datetime import datetime
from pathlib import Path


def start_log(log_file: str, script_name: str):
    """
    Start a new ETL run in the log file.
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    with open(log_file, "a") as f:
        f.write("\n")
        f.write("=" * 80 + "\n")
        f.write(f"RUN DATE : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"SCRIPT   : {script_name}\n")
        f.write("=" * 80 + "\n\n")


def log_error(log_file: str, ticker: str, error):
    """
    Append a failed ticker and its error.
    """
    with open(log_file, "a") as f:
        f.write(f"Ticker : {ticker}\n")
        f.write(f"Error  : {error}\n")
        f.write("-" * 80 + "\n")


def log_record_error(log_file: str, record: str, error):
    """
    Append a failed record and its error.
    """
    with open(log_file, "a") as f:
        f.write(f"Record : {record}\n")
        f.write(f"Error  : {error}\n")
        f.write("-" * 80 + "\n")
        
def end_log(log_file: str, success: int, failed: int):
    """
    Finish the ETL run with a summary.
    """
    with open(log_file, "a") as f:
        f.write("\n")
        f.write(f"Successful : {success}\n")
        f.write(f"Failed     : {failed}\n")
        f.write("=" * 80 + "\n")