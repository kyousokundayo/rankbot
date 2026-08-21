use scripting additions

on containsBot(posixDir)
	try
		set botFile to posixDir & "/bot.py"
		set quotedBotFile to quoted form of botFile
		do shell script ("/usr/bin/test -f " & quotedBotFile)
		return true
	on error
		return false
	end try
end containsBot

set configuredDir to system attribute "WEREWOLF_BOT_DIR"
set publicDir to (POSIX path of (path to desktop folder)) & "rank-werewolf-bot"
set legacyDir to (POSIX path of (path to desktop folder)) & "bot"
if configuredDir is not "" then
	if containsBot(configuredDir) then
		set botDir to configuredDir
	else
		display notification "WEREWOLF_BOT_DIR にBotが見つかりません" with title "人狼Bot"
		return
	end if
else if containsBot(legacyDir) then
	set botDir to legacyDir
else if containsBot(publicDir) then
	set botDir to publicDir
else
	display notification "Botフォルダが見つかりません" with title "人狼Bot"
	return
end if
set launcherFile to botDir & "/scripts/start_bot_detached.py"
set pythonFile to botDir & "/.venv/bin/python"

try
	set shellText to (quoted form of pythonFile) & " " & (quoted form of launcherFile)
	set resultText to (do shell script shellText)
	if resultText contains "already_running" then
		display notification "人狼Bot は既に実行中です" with title "人狼Bot"
	else
		display notification "人狼Bot の準備が完了しました" with title "人狼Bot"
	end if
on error errorMessage
	display notification "起動に失敗しました。launcher.log を確認してください。" with title "人狼Bot"
end try
