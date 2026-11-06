' ============================================================
'  Запуск бота Steam-скидок полностью отвязанным фоновым процессом
'  (без окна консоли). Двойной клик — и бот работает 24/7.
' ============================================================
Option Explicit

Dim fso, shell, here, py, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here

pyw = here & "\.venv\Scripts\pythonw.exe"
py = here & "\.venv\Scripts\python.exe"

If fso.FileExists(pyw) Then
    py = pyw
ElseIf Not fso.FileExists(py) Then
    py = "pythonw.exe"
End If

' 0 = скрытое окно, False = не ждать завершения процесса
shell.Run """" & py & """ run_bot.py", 0, False