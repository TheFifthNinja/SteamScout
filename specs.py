import psutil
import platform
from GPUtil import getGPUs

def get_system_info():
    # CPU
    cpu_name = platform.processor()
    cpu_cores = psutil.cpu_count(logical=True)
    physical_cores = psutil.cpu_count(logical=False)
    
    # RAM 
    ram = psutil.virtual_memory().total / (1024 ** 3)
    
    # OS
    os_info = platform.system() + " " + platform.release()
    
    # GPU
    gpus = getGPUs()
    gpu_info = [gpu.name for gpu in gpus] if gpus else ["No GPU Found"]
    
    system_info = {
        "CPU": f"{cpu_name} ({physical_cores} cores, {cpu_cores} threads)",
        "RAM": f"{ram:.2f} GB",
        "OS": os_info,
        "GPU": ", ".join(gpu_info),
    }
    
    return system_info

if __name__ == "__main__":
    specs = get_system_info()
    print("System Specifications:")
    for key, value in specs.items():
        print(f"{key}: {value}")
