// micro:bit 端範例：配合 robot_bluetooth.py 的指令
// 貼到 MakeCode「JavaScript」分頁再切回積木；需要「藍牙 bluetooth」擴充
// 把每個 if 裡的「顯示箭頭」換成你的馬達積木即可
//
//   F = 前進   B = 後退   L = 左轉   R = 右轉   S = 停止

bluetooth.startUartService()
basic.showIcon(IconNames.Heart)

bluetooth.onBluetoothConnected(function () {
    basic.showIcon(IconNames.Yes)
})

bluetooth.onBluetoothDisconnected(function () {
    basic.showIcon(IconNames.No)
    // TODO：加上「馬達停止」，斷線時一定要停車，避免車子一直往前衝
})

bluetooth.onUartDataReceived(serial.delimiters(Delimiters.Hash), function () {
    let cmd = bluetooth.uartReadUntil(serial.delimiters(Delimiters.Hash))
    if (cmd == "F") {
        led.plot(2, 0)          // TODO：換成「馬達前進」
    } else if (cmd == "B") {
        led.plot(2, 4)          // TODO：換成「馬達後退」
    } else if (cmd == "L") {
        led.plot(0, 2)          // TODO：換成「馬達左轉」
    } else if (cmd == "R") {
        led.plot(4, 2)          // TODO：換成「馬達右轉」
    } else if (cmd == "S") {
        basic.clearScreen()     // TODO：換成「馬達停止」
    }
})
