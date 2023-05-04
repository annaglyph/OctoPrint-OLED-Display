import board
import digitalio
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306
import time
import socket
import argparse
import requests
import datetime
import RPi.GPIO as GPIO
from pyembedded.raspberry_pi_tools.raspberrypi import PI
import os
from pathlib import Path

#######################################
############# COMMANDLINE ARGUMENTS
#######################################

parser = argparse.ArgumentParser()
parser.add_argument('--verbose', action='store_true', default=False, help='Enable verbose telegram messaging')

parser.add_argument('--int-high-temp', type=float, default=65.0, help='Temperature threshold in degrees C to enable fan')
parser.add_argument('--int-low-temp', type=float, default=55.0, help='Temperature threshold in degrees C to disable fan')

parser.add_argument('--delay', type=float, default=10.0, help='Delay, in seconds, between current and next page display')

parser.add_argument('--oled-rotation', type=int, default=2, help='OLED rotation: 0:None // 1:90° // 2:180° // 3:270°')

parser.add_argument('--fan-pin', type=int, default=14, help='GPIO pin number for fan control')
parser.add_argument('--octoprint-pin', type=int, default=21, help='GPIO pin number for OctoPrint override fan control')

parser.add_argument('--dht-pin', type=int, default=24, help='GPIO pin number for DHT22 module')
parser.add_argument('--enable-dht', action='store_true', default=False, help='Enable external temperature & humidity module')
parser.add_argument('--enable-dht-log', action='store_true', default=False, help='Enable logging of temperature & humidity to CSV file')
parser.add_argument('--dht-log-file', type=Path, default='', help='Path to Temperature Humidity log file')

parser.add_argument('--relay-pin', type=int, default=27, help='GPIO pin number for relay control')
parser.add_argument('--enable-relay', action='store_true', default=False, help='Enable external relay module control')
parser.add_argument('--ext-high-temp', type=float, default=60.0, help='Temperature threshold in degrees C to enable relay module')
parser.add_argument('--ext-low-temp', type=float, default=50.0, help='Temperature threshold in degrees C to disable relay module')

parser.add_argument('--octoprint-api-key', type=str, default=False, help='OctoPrint registered application key')

parser.add_argument('--telegram-token', type=str, default='', help='Telegram API token')
parser.add_argument('--telegram-chat-id', type=str, default='', help='Telegram Chat ID')
parser.add_argument('--telegram-message', type=str, default='', help='Telegram message')

parser.add_argument('--screen-height', type=int, default=32, help='OLED screen height')

args = parser.parse_args()

#######################################
############# GLOBALS
#######################################

isONLINE = False
isPRINTING = False
OVERIDE = False
isREBOOT = True

GPIO.setmode(GPIO.BCM) # Use GPIO pin numbering not physical
GPIO.setwarnings(False)
GPIO.setup(args.fan_pin, GPIO.OUT) # Fan control
GPIO.setup(args.octoprint_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN) # Pin controlled by Octoprint

if args.enable_relay:
	print('Setting up external relay control.')
	GPIO.setup(args.relay_pin, GPIO.OUT) # External relay control
	print('Resetting relay pin status: LOW')
	GPIO.output(args.relay_pin, GPIO.LOW)

piStats = PI()


#######################################
############# VARIABLES
#######################################

defaultFont = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 14)

cMessage = "E3v2NEO ONLINE" # Message to show when octoprint is connected
oMessage = "OFFLINE" # Message to show when octoprint is NOT connected
nMessage = "-------" # Message if no Baud / Port is detected

refresh = True # Indicates whether to start with text or blank screen
ap = 1 # Indicates first page to show
apMax = 7 # Indicates number of pages
skip = False # Indicates we are skipping page


#######################################
############# SET API END-POINTS
#######################################

urlCon = 'http://localhost/api/connection?apikey='
urlJob = 'http://localhost/api/job?apikey='
#urlPrn = 'http://localhost/api/printer?history=true&limit=2&apikey='
urlPrn = 'http://localhost/api/printer?history=false&apikey='


#######################################
############# FUNCTIONS
#######################################

def clearDisplay():
	draw.rectangle((0, 0, oled.width-1, oled.height-1), outline=0, fill=0)
	oled.image(image)
	oled.show()


#######################################
############# GET JOB STATUS
#######################################

def getJobStatus():
	opResponse = requests.get(urlJob + args.octoprint_api_key)
	opStatus = opResponse.json()
	
	timeNow = datetime.datetime.now().time()
	
	printTimeLeft = opStatus.get('progress', {}).get('printTimeLeft', "") # Get print time left
	
	fulldate = datetime.datetime(100, 1, 1, timeNow.hour, timeNow.minute, timeNow.second)
	fulldate = fulldate + datetime.timedelta(seconds=printTimeLeft)
	completion = opStatus.get('progress', {}).get('completion', "") # Get print time left
	
	return {"eta": fulldate.strftime("%H:%M"), "completion": completion}
	
#######################################
############# GET PRINTER STATUS
#######################################

def getPrinterStatus():
	opResponse = requests.get(urlPrn + args.octoprint_api_key)
	opStatus = opResponse.json()

	bedTemp = opStatus.get('temperature', {}).get('bed', {}).get('actual', "")
	toolTemp = opStatus.get('temperature', {}).get('tool0', {}).get('actual', "")

	return {"bedTemp": bedTemp, "toolTemp": toolTemp}


#######################################
############# GET CONNECTION STATUS
#######################################

def getConectionStatus():
	global isONLINE
	global isPRINTING
	opResponse = requests.get(urlCon + args.octoprint_api_key)
	opStatus = opResponse.json()

	pState = opStatus.get('current', {}).get('state', "") # Get Printer Status	- Typically Closed or Operational
	if not pState or pState == "Closed":
		pState = oMessage # Offline text to display
		isONLINE = False
		isPRINTING = False
	elif pState == "Operational":
		pState = cMessage
		isONLINE = True
		isPRINTING = False
	elif pState == "Printing":
		isONLINE = True
		isPRINTING = True

	pBaudrate = opStatus.get('current', {}).get('baudrate', "")
	if not pBaudrate:
		pBaudrate = nMessage
	else:
		pBaudrate = str(pBaudrate)

	pPort = opStatus.get('current', {}).get('port', "")
	if not pPort:
		pPort = nMessage

	return {"pState": pState, "pPort": pPort, "pBaudrate": pBaudrate}
	

#######################################
############# GET EXTERNAL HUMIDITY & TEMPERATURE
#######################################

def getExternalTempHumidity():
	ext_temperature = None
	ext_humidity = None
		
	while ext_temperature is None and ext_humidity is None:
		try:
			#print('Attempting to get external temperature and humidity.')
			ext_temperature = dhtDevice.temperature
			ext_humidity = dhtDevice.humidity
			
			if ext_temperature is not None and ext_humidity is not None:
				#print("Temp: {:.1f} C	 Humidity: {}% ".format(ext_temperature, ext_humidity))
				if args.enable_dht_log:
					dht_csv_filepath.write('{0},{1:0.1f},{2:0.1f}\r\n'.format(time.strftime('%m/%d/%y %H:%M:%S'), ext_temperature, ext_humidity))
					dht_csv_filepath.flush()
					os.fsync(dht_csv_filepath.fileno())
				return {"ext_temperature": float(ext_temperature), "ext_humidity": float(ext_humidity)}

		except RuntimeError as error:
			# Errors happen fairly often, DHT's are hard to read, just keep going
			#print('RuntimeError: ', error.args[0])
			time.sleep(2)

		except Exception as error:
			dhtDevice.exit()
			raise error
	
	
#######################################
############# DHT22 SETUP
#######################################

if args.enable_dht:
	print('Setting up DHT22.')
	import adafruit_dht

	# Initial the dht device, with data pin connected to:
	#dhtDevice = adafruit_dht.DHT22(board.D24)

	# you can pass DHT22 use_pulseio=False if you wouldn't like to use pulseio.
	# This may be necessary on a Linux single board computer like the Raspberry Pi,
	# but it will not work in CircuitPython.
	dhtDevice = adafruit_dht.DHT22(board.D24, use_pulseio=False)

if args.enable_dht_log:
	try:
		dht_csv_filepath = open(args.dht_log_file, 'a+')
		if os.stat(args.dht_log_file).st_size == 0:
			dht_csv_filepath.write('Date,Temperature,Humidity\r\n')
	except:
		pass


#######################################
############# OLED SETUP
#######################################

oled_reset = digitalio.DigitalInOut(board.D4)  # Define the Reset Pin
WIDTH = 128
HEIGHT = args.screen_height	 # Change to 64 if needed
i2c = board.I2C()
oled = adafruit_ssd1306.SSD1306_I2C(WIDTH, HEIGHT, i2c, addr=0x3C, reset=oled_reset)
oled.rotation = args.oled_rotation

# Create blank image for drawing.
# Make sure to create image with mode '1' for 1-bit color.
image = Image.new("1", (oled.width, oled.height))
draw = ImageDraw.Draw(image) # Get drawing object to draw on image.


#######################################
############# OLED LOOP
#######################################

clearDisplay()
timerStartValue = time.time()


while True:
	if isREBOOT == True and args.verbose:
		url = f"https://api.telegram.org/bot{args.telegram_token}/sendMessage?chat_id={args.telegram_chat_id}&text={args.telegram_message}"
		requests.get(url)
		isREBOOT = False

	fanPinStatus = GPIO.input(args.fan_pin)
	octoPrintFanControlPinStatus = GPIO.input(args.octoprint_pin)
	
	cpuTemp = piStats.get_cpu_temp()
	if cpuTemp > args.int_high_temp and fanPinStatus == 0:
		OVERIDE = True
		GPIO.output(args.fan_pin, GPIO.HIGH)
		
		message = 'COOLING'
		messageFont = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 22)
		(messageFontWidth, messageFontHeight) = messageFont.getsize(message)
		
		clearDisplay()
		
		draw.text((oled.width // 2 - messageFontWidth // 2, 3), message, font=messageFont, fill=255)
		oled.image(image)
		oled.show()
		
		time.sleep(10)

	if cpuTemp < args.int_low_temp and fanPinStatus == 1:
		OVERIDE = False
		GPIO.output(args.fan_pin, GPIO.LOW)

	if fanPinStatus == 0 and octoPrintFanControlPinStatus == 1:
		GPIO.output(args.fan_pin, GPIO.HIGH)
	
	if fanPinStatus == 1 and octoPrintFanControlPinStatus == 0 and not OVERIDE:
		GPIO.output(args.fan_pin, GPIO.LOW)
	
	timePassed = time.time() - timerStartValue # Monitor elapsed time
	if timePassed > args.delay or skip == True: # If elapsed time is greater than our page delay
		ap += 1 # Increase to the next page number
			
		if not args.octoprint_api_key and ap==4:
			ap += 1 # Increase to the next page number
		
		if not isONLINE and ap==5:
			ap += 1 # Increase to the next page number
			
		if not isPRINTING and ap==6:
			ap += 1 # Increase to the next page number
			
		if not args.enable_dht and ap==7:
			ap += 1 # Increase to the next page number
			
		if ap > apMax: # If we are beyond our max pages
			ap = 1 # Go back to our first page
			
		timerStartValue = time.time()
		refresh = True # Indicate we need to refresh page

	#### PAGE ONE ####
	if ap == 1 and refresh:
		ipAddress = piStats.get_connected_ip_addr(network='eth0')
		hostName = socket.gethostname()
		
		(topLineFontWidth, topLineFontHeight) = defaultFont.getsize(ipAddress)
		(bottomLineFontWidth, bottomLineFontHeight) = defaultFont.getsize(hostName)
		
		clearDisplay()
		
		draw.text((oled.width // 2 - topLineFontWidth // 2, -2), ipAddress, font=defaultFont, fill=255)
		draw.text((oled.width // 2 - bottomLineFontWidth // 2 + 1, 16), hostName, font=defaultFont, fill=255)
		
		oled.image(image)
		oled.show()
		refresh = False
		
	#### PAGE TWO ####
	if ap == 2 and refresh:
		font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 10)
		cpuLoad = "CPU: {}%".format(piStats.get_cpu_usage())

		getMU = piStats.get_ram_info()
		memoryUsage = "RAM: {} / {} GB {}%".format(round(int(getMU[1])/1024000,3), round(int(getMU[0])/1024000,1) , int(int(getMU[1])*100/int(getMU[0])))

		getDS = piStats.get_disk_space()
		diskUsage = "HHD: {} / {} GB {}".format(getDS[1][:-1], getDS[0][:-1], getDS[3])

		clearDisplay()
		
		draw.text((0, -2), cpuLoad, font=font, fill=255)
		draw.text((0, 9), memoryUsage, font=font, fill=255)
		draw.text((0, 21), diskUsage, font=font, fill=255)
		
		oled.image(image)
		oled.show()
		refresh = False

	#### PAGE THREE ####
	if ap == 3 and refresh:
		piTemp = round(cpuTemp,1)
		piTempStr = str(piTemp) + "°c"
		
		font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 22)
		(font_width, font_height) = font.getsize(piTempStr)
		
		clearDisplay()
		
		draw.text((oled.width // 2 - font_width // 2, 3), piTempStr, font=font, fill=255)
		
		oled.image(image)
		oled.show()
		refresh = False

	#### PAGE FOUR ####
	if ap == 4 and refresh:
		pCon = getConectionStatus()
		(topLineFontWidth, topLineFontHeighteight) = defaultFont.getsize(pCon["pState"])
		
		clearDisplay()

		draw.text((oled.width // 2 - topLineFontWidth // 2, 0), pCon["pState"], font=defaultFont, fill=255)
		for i in range(4):
			font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 12)
			(font_width, font_height) = font.getsize(pCon["pPort"])
			draw.text((oled.width // 2 - font_width // 2, 16), pCon["pPort"], font=font, fill=255)
			oled.image(image)
			oled.show()
			time.sleep(4)
	
			font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 12)
			(font_width, font_height) = font.getsize(pCon["pBaudrate"])
			draw.rectangle((0, 16, oled.width-1, oled.height-1), outline=0, fill=0)
			oled.image(image)
			oled.show()
			draw.text((oled.width // 2 - font_width // 2, 16), pCon["pBaudrate"], font=font, fill=255)
			oled.image(image)
			oled.show()
			time.sleep(4)
			
			draw.rectangle((0, 16, oled.width-1, oled.height-1), outline=0, fill=0)
			oled.image(image)
			oled.show()
		refresh = False

	#### PAGE FIVE ####
	if ap == 5 and refresh and isONLINE:
		pPrn = getPrinterStatus()
		bedTempStr = "Bed: " + str(round(pPrn["bedTemp"],2)) + "°c"
		toolTempStr = "Tool: " + str(round(pPrn["toolTemp"],2)) + "°c"
		
		clearDisplay()

		draw.text((17, 0), bedTempStr, font=defaultFont, fill=255)
		draw.text((15, 16), toolTempStr, font=defaultFont, fill=255)
		
		oled.image(image)
		oled.show()
		refresh = False
		
	#### PAGE SIX ####
	if ap == 6 and refresh and isPRINTING:
		pJob = getJobStatus()
		progress = "Gcode: {}%".format(round(pJob["completion"],1))
		eta = "ETA: {}".format(pJob["eta"])
		
		clearDisplay()
		
		draw.text((10, 0), progress, font=defaultFont, fill=255)
		draw.text((28, 16), eta, font=defaultFont, fill=255)
		
		oled.image(image)
		oled.show()
		refresh = False
		
	#### PAGE SEVEN ####
	if ap == 7 and refresh:
		dhtData = getExternalTempHumidity()
		extTemperatureStr = "Ext. Temp: " + str(dhtData["ext_temperature"]) + "°c"
		extHumidityStr = "Ext. RelH: " + str(dhtData["ext_humidity"]) + "%"
		
		clearDisplay()

		draw.text((0, 0), extTemperatureStr, font=defaultFont, fill=255)
		draw.text((8, 16), extHumidityStr, font=defaultFont, fill=255)
		
		oled.image(image)
		oled.show()
		
		if args.enable_relay:
			relayPinStatus = GPIO.input(args.relay_pin)
		
			if dhtData["ext_temperature"] > args.ext_high_temp and relayPinStatus == 0:
				print('Turning on relay.')
				if args.enable_dht_log:
					dht_csv_filepath.write('{0},{1:0.1f},{2:0.1f},ENABLED\r\n'.format(time.strftime('%m/%d/%y %H:%M:%S'), dhtData["ext_temperature"], dhtData["ext_humidity"]))
					dht_csv_filepath.flush()
					os.fsync(dht_csv_filepath.fileno())
					
				GPIO.output(args.relay_pin, GPIO.HIGH)
				
				
				if args.verbose:
					telegramMessage = 'External fan enabled.'
					url = f"https://api.telegram.org/bot{args.telegram_token}/sendMessage?chat_id={args.telegram_chat_id}&text={telegramMessage}"
					requests.get(url)
		
				relayMessage = 'EXT. COOLING'
				relayMessageFont = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
				(relayMessageFontWidth, relayMessageFontHeight) = relayMessageFont.getsize(relayMessage)
		
				clearDisplay()
		
				draw.text((oled.width // 2 - relayMessageFontWidth // 2, 3), relayMessage, font=relayMessageFont, fill=255)
				oled.image(image)
				oled.show()
		
				#time.sleep(10)

			if dhtData["ext_temperature"] < args.ext_low_temp and relayPinStatus == 1:
				print('Turning off relay.')
				if args.enable_dht_log:
					dht_csv_filepath.write('{0},{1:0.1f},{2:0.1f},DISABLED\r\n'.format(time.strftime('%m/%d/%y %H:%M:%S'), dhtData["ext_temperature"], dhtData["ext_humidity"]))
					dht_csv_filepath.flush()
					os.fsync(dht_csv_filepath.fileno())
				GPIO.output(args.relay_pin, GPIO.LOW)
				if args.verbose:
					telegramMessage = 'External fan disabled.'
					url = f"https://api.telegram.org/bot{args.telegram_token}/sendMessage?chat_id={args.telegram_chat_id}&text={telegramMessage}"
					requests.get(url)
				
			if relayPinStatus == 1:
				#time.sleep(5)
				relayMessage = 'EXT. FAN ON'
				relayMessageFont = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 16)
				(relayMessageFontWidth, relayMessageFontHeight) = relayMessageFont.getsize(relayMessage)
	
				clearDisplay()
	
				draw.text((oled.width // 2 - relayMessageFontWidth // 2, 3), relayMessage, font=relayMessageFont, fill=255)
				oled.image(image)
				oled.show()
				
				time.sleep(5)
			
		refresh = False
				
		
	time.sleep(2)
