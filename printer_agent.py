import os
import time
import win32print
import win32api

PRINT_QUEUE_FOLDER = 'print_queue'

print("Auto Printer Agent সফলভাবে চালু হয়েছে। প্রিন্ট কিউ ফোল্ডার মনিটর করা হচ্ছে...")

while True:
    try:
        if os.path.exists(PRINT_QUEUE_FOLDER):
            files = os.listdir(PRINT_QUEUE_FOLDER)
            for file in files:
                filepath = os.path.join(PRINT_QUEUE_FOLDER, file)
                if os.path.isfile(filepath):
                    print(f"নতুন প্রিন্ট জব পাওয়া গেছে: {file}")
                    
                    # উইন্ডোজের ডিফল্ট প্রিন্টার দিয়ে সরাসরি প্রিন্ট কমান্ড পাঠানো
                    win32api.ShellExecute(0, "print", os.path.abspath(filepath), None, ".", 0)
                    
                    # প্রিন্ট প্রসেস হওয়ার জন্য ১০ সেকেন্ড অপেক্ষা করা
                    time.sleep(10)
                    
                    # সফলভাবে প্রিন্ট হওয়ার পর কিউ ফোল্ডার থেকে ফাইল ডিলিট করা
                    os.remove(filepath)
                    print("প্রিন্ট সফলভাবে সম্পন্ন হয়েছে এবং ফাইল মুছে ফেলা হয়েছে!")
    except Exception as e:
        print("প্রিন্ট করতে গিয়ে সমস্যা হয়েছে:", e)
        
    time.sleep(3)