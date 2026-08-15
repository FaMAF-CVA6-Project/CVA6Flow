import os
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed

# ==========================================
# Configuration
# ==========================================
# Adjust this number based on your CPU cores and available RAM.
# 4 is a good balance to avoid overloading a standard 8-core machine.
MAX_WORKERS = 4
TRACER_SCRIPT = "../CVA6Flow_tracer.py"


def process_vcd(vcd_file):
    """
    Executes the tracer for a single VCD file if its 
    corresponding .list file exists.
    """
    # Extract the base name. Example: "daxpy.config10.vcd" -> "daxpy.config10"
    base_name = vcd_file.rsplit('.vcd', 1)[0]
    list_file = f"{base_name}.list"
    json_file = f"{base_name}.json"

    # Validate that the disassembly list file exists
    if not os.path.exists(list_file):
        return f"[SKIPPED] List file not found for: {vcd_file}"

    # Prepare the exact command requested
    cmd = [
        "python3", TRACER_SCRIPT,
        vcd_file,
        "--disasm-list", list_file,
        "-o", json_file
    ]

    try:
        # Execute the subprocess
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return f"[SUCCESS] Generated: {json_file}"
    except subprocess.CalledProcessError as e:
        # If the tracer fails, catch the error without stopping other processes
        return f"[ERROR] Failed on {vcd_file}:\n{e.stderr}"


def main():
    # List all files in the current directory ending in .vcd
    vcd_files = [f for f in os.listdir('.') if f.endswith('.vcd')]

    if not vcd_files:
        print("No .vcd files found in the current directory.")
        return

    print(f"Found {len(vcd_files)} .vcd files to process.")
    print(f"Starting concurrent processing with {MAX_WORKERS} workers...\n")

    # Use ProcessPoolExecutor to handle concurrency
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks to the pool
        futures = {executor.submit(process_vcd, vcd): vcd for vcd in vcd_files}

        # Print results as each task completes
        for future in as_completed(futures):
            print(future.result())

    print("\nBatch processing finished.")

if __name__ == "__main__":
    main()
