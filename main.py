# Estación meteorológica con ESP32 (MicroPython).
# Lee DHT11, BMP180 y un sensor de lluvia YL-38/YL-83, enciende buzzer y LED
# cuando llueve y sirve un panel web en el puerto 80.

from machine import Pin, I2C
import time, network, socket, json
import dht, bmp180

try:
    from config import WIFI_SSID, WIFI_PASSWORD
except ImportError:
    raise SystemExit("Falta config.py: copia config.example.py y pon tu red")

# ---- Pines ----
PIN_I2C_SCL = 22
PIN_I2C_SDA = 21
PIN_DHT     = 4
PIN_RAIN_DO = 34   # salida digital del YL-38 (0 = mojado)
PIN_BUZZER  = 16   # buzzer activo por nivel bajo (0 = suena)
PIN_LED     = 2
PIN_EMERG   = 13   # botón de paro, con pull-up (0 = presionado)

# ---- Sensores ----
i2c = I2C(0, scl=Pin(PIN_I2C_SCL), sda=Pin(PIN_I2C_SDA), freq=100000)
bmp = bmp180.BMP180(i2c)
bmp.oversample = 2
bmp.sealevel = 101325  # Pa

sensor_dht = dht.DHT11(Pin(PIN_DHT))
lluvia_pin = Pin(PIN_RAIN_DO, Pin.IN)

# ---- Actuadores ----
buzzer = Pin(PIN_BUZZER, Pin.OUT, value=1)  # apagado
led    = Pin(PIN_LED, Pin.OUT, value=0)
boton_emergencia = Pin(PIN_EMERG, Pin.IN, Pin.PULL_UP)

# Se cambian desde el panel web
buzzer_enabled = True
led_enabled    = True


def leer_sensores():
    try:
        sensor_dht.measure()
        temp = sensor_dht.temperature()
        hum = sensor_dht.humidity()
    except OSError as e:
        print("Error DHT11:", e)
        temp = hum = None

    try:
        temp_bmp = bmp.temperature
        pres_hpa = bmp.pressure / 100.0
    except OSError as e:
        print("Error BMP180:", e)
        temp_bmp = pres_hpa = None

    lloviendo = lluvia_pin.value() == 0
    return temp, hum, pres_hpa, lloviendo, temp_bmp


def emergencia_activada():
    return boton_emergencia.value() == 0


def actualizar_actuadores(lloviendo, e_stop):
    # Con el paro activo todo queda apagado, sin importar lo demás
    sonar = lloviendo and buzzer_enabled and not e_stop
    prender = lloviendo and led_enabled and not e_stop
    buzzer.value(0 if sonar else 1)
    led.value(1 if prender else 0)


# ---- HTTP ----

def send_all(conn, data):
    # send() puede mandar menos bytes de los pedidos; seguimos hasta terminar
    mv = memoryview(data)
    while mv:
        n = conn.send(mv)
        mv = mv[n:]


def send_json(conn, payload):
    body = json.dumps(payload)
    send_all(conn, (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n" + body
    ).encode())


def send_page(conn):
    send_all(conn, b"HTTP/1.1 200 OK\r\n"
                   b"Content-Type: text/html; charset=utf-8\r\n"
                   b"Connection: close\r\n\r\n")
    # El HTML vive en flash; se manda por partes para no llenar la RAM
    with open("index.html", "rb") as f:
        while True:
            chunk = f.read(1024)
            if not chunk:
                break
            send_all(conn, chunk)


def send_not_found(conn):
    send_all(conn, b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")


def estado_salidas():
    return {"buzzer_enabled": buzzer_enabled, "led_enabled": led_enabled}


def handle_data(conn):
    temp, hum, pres_hpa, lloviendo, temp_bmp = leer_sensores()
    e_stop = emergencia_activada()
    actualizar_actuadores(lloviendo, e_stop)

    payload = {
        "temp_dht": temp,
        "temp_bmp": temp_bmp,
        "hum": hum,
        "pres_hpa": pres_hpa,
        "lluvia": lloviendo,
        "emergency": e_stop,
    }
    payload.update(estado_salidas())
    send_json(conn, payload)


def handle_control(conn, query):
    # /control?buzzer=toggle  o  /control?led=toggle
    global buzzer_enabled, led_enabled
    for param in query.split("&"):
        if param == "buzzer=toggle":
            buzzer_enabled = not buzzer_enabled
        elif param == "led=toggle":
            led_enabled = not led_enabled
    send_json(conn, estado_salidas())


def parse_path(request):
    # Primera línea: "GET /control?led=toggle HTTP/1.1"
    try:
        target = request.split(b"\r\n", 1)[0].split(b" ")[1].decode()
    except (IndexError, UnicodeError):
        return "/", ""
    path, _, query = target.partition("?")
    return path, query


def connect_wifi(timeout_s=20):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Conectando a", WIFI_SSID, "...")
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        inicio = time.time()
        while not wlan.isconnected():
            if time.time() - inicio > timeout_s:
                raise RuntimeError("No se pudo conectar al WiFi")
            time.sleep(0.5)
    print("Conectado. Abre http://%s" % wlan.ifconfig()[0])


def run_server():
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except (AttributeError, OSError):
        pass  # no todos los firmwares lo tienen
    s.bind(addr)
    s.listen(5)
    print("Servidor escuchando en el puerto 80")

    try:
        while True:
            try:
                conn, _ = s.accept()
            except OSError as e:
                print("Error en accept:", e)
                continue

            try:
                request = conn.recv(1024)
                if not request:
                    continue
                path, query = parse_path(request)
                if path == "/data":
                    handle_data(conn)
                elif path == "/control":
                    handle_control(conn, query)
                elif path == "/":
                    send_page(conn)
                else:
                    send_not_found(conn)
            except OSError as e:
                # ECONNRESET cuando el navegador cierra antes de tiempo
                print("Error de conexión:", e)
            finally:
                conn.close()
    finally:
        s.close()


connect_wifi()
run_server()
