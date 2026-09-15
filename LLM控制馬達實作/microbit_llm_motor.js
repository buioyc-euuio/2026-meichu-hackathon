/**
 * micro:bit 端：讓 LLM 透過藍牙控制輪子與相機
 * 配合同資料夾的 robot_bluetooth.py
 *
 * 貼到 MakeCode「JavaScript」分頁；需要「藍牙 bluetooth」擴充和原本的 sensors 擴充
 * 專案設定記得選 No Pairing Required
 *
 * 電腦 → micro:bit 指令（結尾都是 #）：
 *   M,左輪,右輪#   -255~255：正數=前進、負數=後退、0=停
 *   Z,速度#        相機上下馬達 -255~255：正數=往上、負數=往下
 *   P,角度#        相機左右伺服馬達 0~180：0=最右、90=正前方、180=最左
 *   S#             全部馬達停止
 *
 * 安全機制：超過 0.5 秒沒收到指令，輪子和相機上下馬達自動停止
 */

// ====== 你原本的函式（沒有修改）======
// turn=1 往前走 / 往上升，turn=0 往後走 / 往下降，speed 0~255
function left_wheel (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P0,
    (left_wheel_forward + turn + 1) % 2,
    AnalogPin.P16,
    speed
    )
}
function right_wheel (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P2,
    (right_wheel_forward + turn + 1) % 2,
    AnalogPin.P1,
    speed
    )
}
function camara_z (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P15,
    (camara_z_up + turn + 1) % 2,
    AnalogPin.P14,
    speed
    )
}
// 0 最右、180 最左
function servo (angle: number) {
    pins.servoWritePin(AnalogPin.P8, angle)
}

// ====== 把「有正負號的速度」轉成 turn + speed ======
function run_left (value: number) {
    if (value >= 0) {
        left_wheel(1, Math.min(value, 255))
    } else {
        left_wheel(0, Math.min(0 - value, 255))
    }
}
function run_right (value: number) {
    if (value >= 0) {
        right_wheel(1, Math.min(value, 255))
    } else {
        right_wheel(0, Math.min(0 - value, 255))
    }
}
function run_camera_z (value: number) {
    if (value >= 0) {
        camara_z(1, Math.min(value, 255))
    } else {
        camara_z(0, Math.min(0 - value, 255))
    }
}
function stop_all () {
    left_wheel(1, 0)
    right_wheel(1, 0)
    camara_z(1, 0)
    motors_on = false
}

// ====== 藍牙 ======
bluetooth.onBluetoothConnected(function () {
    basic.showIcon(IconNames.Yes)
})
bluetooth.onBluetoothDisconnected(function () {
    stop_all()
    basic.showIcon(IconNames.No)
})

bluetooth.onUartDataReceived(serial.delimiters(Delimiters.Hash), function () {
    cmd = bluetooth.uartReadUntil(serial.delimiters(Delimiters.Hash))
    parts = cmd.split(",")
    last_cmd_time = input.runningTime()
    if (parts[0] == "M") {
        run_left(parseInt(parts[1]))
        run_right(parseInt(parts[2]))
        motors_on = true
    } else if (parts[0] == "Z") {
        run_camera_z(parseInt(parts[1]))
        motors_on = true
    } else if (parts[0] == "P") {
        servo(Math.constrain(parseInt(parts[1]), 0, 180))
    } else if (parts[0] == "S") {
        stop_all()
    }
})

// 安全機制：0.5 秒沒收到新指令就停車（電腦在動作中每 0.1 秒會重送一次）
basic.forever(function () {
    if (motors_on && input.runningTime() - last_cmd_time > 500) {
        stop_all()
    }
    basic.pause(50)
})

// ====== 按鈕：手動測試相機左右（A 看左、B 看右、A+B 回正前方）======
input.onButtonPressed(Button.A, function () {
    servo(180)
})
input.onButtonPressed(Button.B, function () {
    servo(0)
})
input.onButtonPressed(Button.AB, function () {
    servo(90)
})

// ====== 開機設定 ======
let cmd = ""
let parts: string[] = []
let last_cmd_time = 0
let motors_on = false
let camara_z_up = 0
let right_wheel_forward = 0
let left_wheel_forward = 0
left_wheel_forward = 0
right_wheel_forward = 1
camara_z_up = 0
stop_all()
servo(90)
bluetooth.startUartService()
basic.showIcon(IconNames.Heart)
