#include <Encoder.h>

Encoder myEnc(0, 1);

void setup() {
	delay(1000);
  Serial.begin(9600);
  delay(100);
}

uint32_t tPrint = 0;

void loop() {

	if ((millis() - tPrint) > 50) {
		tPrint  = millis();
		Serial.println(abs(myEnc.readAndReset()));
	}
	
  
}