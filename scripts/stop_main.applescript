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
if configuredDir is not "" and containsBot(configuredDir) then
	set botDir to configuredDir
else if containsBot(legacyDir) then
	set botDir to legacyDir
else if containsBot(publicDir) then
	set botDir to publicDir
else
	display notification "Botフォルダが見つかりません" with title "人狼Bot"
	return
end if
set stopFile to botDir & "/scripts/stop_bot.sh"

try
	set shellText to "/bin/zsh " & (quoted form of stopFile)
	set resultText to (do shell script shellText)
	if resultText is "not_running" then
		display notification "人狼Bot は実行されていません" with title "人狼Bot"
	else if resultText is "stopped" then
		display notification "人狼Bot を停止しました" with title "人狼Bot"
	else if resultText is "stopped_force" then
		display notification "人狼Bot を強制停止しました" with title "人狼Bot"
	else
		display notification "人狼Bot の停止に失敗しました。プロセスを確認してください。" with title "人狼Bot"
	end if
on error errorMessage
	display notification "人狼Bot の停止に失敗しました。プロセスを確認してください。" with title "人狼Bot"
end try
