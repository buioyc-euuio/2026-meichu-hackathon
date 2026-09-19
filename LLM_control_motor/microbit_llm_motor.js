/**
 * micro:bit 端：讓 LLM 透過藍牙控制輪子與相機
 * 
 * 配合同資料夾的 robot_bluetooth.py
 * 
 * 貼到 MakeCode「JavaScript」分頁；需要「藍牙 bluetooth」擴充和原本的 sensors 擴充
 * 
 * 專案設定記得選 No Pairing Required
 * 
 * 電腦 → micro:bit 指令（結尾都是 #）：
 * 
 * M,左輪,右輪#   -255~255：正數=前進、負數=後退、0=停
 * 
 * Z,速度#        相機上下馬達 -255~255：正數=往上、負數=往下
 * 
 * P,角度#        相機左右伺服馬達 0~180：0=最左、90=正前方、180=最右
 * 
 * T,角度#        尾巴伺服馬達 0~180：90=中間（會中斷搖尾巴）
 * 
 * W,次數,毫秒,幅度#  搖尾巴：左右來回「次數」下，每擺一邊停「毫秒」，以 90 為中心左右各擺「幅度」度
 * 
 * S#             全部馬達停止（尾巴也停止搖動、回到中間）
 * 
 * 安全機制：超過 0.5 秒沒收到指令，輪子和相機上下馬達自動停止
 * （搖尾巴在背景自己跑完，不受這個安全機制影響，所以可以一邊搖尾巴一邊轉圈）
 */
// ====== 開機設定 ======
function run_camera_z (value: number) {
    if (value >= 0) {
        camara_z(1, Math.min(value, 255))
    } else {
        camara_z(0, Math.min(0 - value, 255))
    }
}
// ====== 你原本的函式（沒有修改）======
// turn=1 往前走 / 往上升，turn=0 往後走 / 往下降，speed 0~255
function left_wheel (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P2,
    (left_wheel_forward + turn + 1) % 2,
    AnalogPin.P1,
    speed
    )
}
// ====== 藍牙 ======
bluetooth.onBluetoothConnected(function () {
    basic.showIcon(IconNames.Yes)
})
bluetooth.onBluetoothDisconnected(function () {
    stop_all()
    stop_tail()
    basic.showIcon(IconNames.No)
})
// ====== 按鈕：手動測試相機左右 ======
input.onButtonPressed(Button.A, function () {
    camera_servo(180)
})
function camara_z (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P15,
    (camara_z_up + turn + 1) % 2,
    AnalogPin.P14,
    speed
    )
}
// ====== 把「有正負號的速度」轉成 turn + speed ======
function run_left (value: number) {
    if (value >= 0) {
        left_wheel(1, Math.min(value, 255))
    } else {
        left_wheel(0, Math.min(0 - value, 255))
    }
}
function stop_all () {
    left_wheel(1, 0)
    right_wheel(1, 0)
    camara_z(1, 0)
    motors_on = false
}
function right_wheel (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P0,
    (right_wheel_forward + turn + 1) % 2,
    AnalogPin.P16,
    speed
    )
}
function run_right (value: number) {
    if (value >= 0) {
        right_wheel(1, Math.min(value, 255))
    } else {
        right_wheel(0, Math.min(0 - value, 255))
    }
}
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
        camera_servo(Math.constrain(parseInt(parts[1]), 0, 180))
    } else if (parts[0] == "T") {
        wag_left = 0
        tail_servo(Math.constrain(parseInt(parts[1]), 0, 180))
    } else if (parts[0] == "W") {
        wag_ms = Math.constrain(parseInt(parts[2]), 80, 1000)
        wag_amp = Math.constrain(parseInt(parts[3]), 5, 90)
        wag_left = Math.constrain(parseInt(parts[1]), 0, 50)
    } else if (parts[0] == "S") {
        stop_all()
        stop_tail()
    }
})
input.onButtonPressed(Button.AB, function () {
    camera_servo(90)
})
input.onButtonPressed(Button.B, function () {
    camera_servo(0)
})
// 0 最左、180 最右
function tail_servo (angle: number) {
    pins.servoWritePin(AnalogPin.P13, angle)
}
function stop_tail () {
    wag_left = 0
    tail_servo(90)
}
// 0 最左、180 最右
function camera_servo (angle: number) {
    pins.servoWritePin(AnalogPin.P8, angle)
}
let wag_amp = 40
let wag_ms = 200
let wag_left = 0
let last_cmd_time = 0
let parts: string[] = []
let cmd = ""
let motors_on = false
let camara_z_up = 0
let right_wheel_forward = 0
let left_wheel_forward = 0
left_wheel_forward = 0
right_wheel_forward = 1
camara_z_up = 0
stop_all()
camera_servo(90)
tail_servo(90)
bluetooth.startUartService()
basic.showIcon(IconNames.Heart)
// 安全機制：0.5 秒沒收到新指令就停車（電腦在動作中每 0.1 秒會重送一次）
basic.forever(function () {
    if (motors_on && input.runningTime() - last_cmd_time > 500) {
        stop_all()
    }
    basic.pause(50)
})
// 搖尾巴：在背景一下一下擺，收到 T 或 S（wag_left 變 0）就不再擺
basic.forever(function () {
    if (wag_left > 0) {
        tail_servo(90 + wag_amp)
        basic.pause(wag_ms)
    }
    if (wag_left > 0) {
        tail_servo(90 - wag_amp)
        basic.pause(wag_ms)
    }
    if (wag_left > 0) {
        wag_left += -1
        if (wag_left == 0) {
            tail_servo(90)
        }
    }
    basic.pause(20)
})
