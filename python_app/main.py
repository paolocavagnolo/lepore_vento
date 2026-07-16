import RPi.GPIO as GPIO
import time

# Configurazione pin
PIN_A = 14
PIN_B = 15

# Variabili globali
counter = 0

def setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN_A, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_B, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    
    # Interrupt su entrambi i canali
    GPIO.add_event_detect(PIN_A, GPIO.RISING, callback=on_encoder_change)
    GPIO.add_event_detect(PIN_B, GPIO.RISING, callback=on_encoder_change)

def on_encoder_change(channel):
    global counter
    
    state_a = GPIO.input(PIN_A)
    state_b = GPIO.input(PIN_B)
    
    if state_a == state_b:
        counter += 1
    else:
        counter -= 1

def get_giri():
    return counter / 600

def cleanup():
    GPIO.cleanup()

if __name__ == "__main__":
    try:
        setup()
        print("Encoder in ascolto... (Premi Ctrl+C per fermare)")
        
        while True:
            print(f"Impulsi: {counter} | Giri: {get_giri():.2f}")
            time.sleep(1)
    
    except KeyboardInterrupt:
        print("\nFermo...")
    finally:
        cleanup()
