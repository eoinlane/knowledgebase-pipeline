-- Exports selected Apple Calendar calendars to /tmp/cal_*.txt
-- Uses batch property fetching (3 Apple Events per calendar, not 3 per event).
-- Calendar must be running before this script is called.

-- {calendar name, match count (-1 = any), output file}
set calMappings to {¬
    {"Eoin Lane", -1, "/tmp/cal_eoinlane.txt"}, ¬
    {"Work", -1, "/tmp/cal_work.txt"}, ¬
    {"eoin@novalconsultancy.com", -1, "/tmp/calendar_events.txt"}, ¬
    {"Personal", -1, "/tmp/cal_personal.txt"}, ¬
    {"Home", -1, "/tmp/cal_home.txt"}, ¬
    {"Calendar", 203, "/tmp/cal_extra_15.txt"}, ¬
    {"Calendar", 251, "/tmp/cal_nta.txt"} ¬
}

set logLines to {}

tell application "Calendar"

    repeat with mapping in calMappings
        set targetName to item 1 of mapping
        set targetCount to item 2 of mapping
        set outPath to item 3 of mapping

        -- Find matching calendar
        set targetCal to missing value
        repeat with c in every calendar
            if name of c is targetName then
                set evtCount to count of every event of c
                if targetCount is -1 then
                    set targetCal to c
                    exit repeat
                else if evtCount > (targetCount - 20) and evtCount < (targetCount + 20) then
                    set targetCal to c
                    exit repeat
                end if
            end if
        end repeat

        if targetCal is missing value then
            set end of logLines to "SKIP: " & targetName
        else
            set evtCount to count of every event of targetCal
            set end of logLines to "EXPORTING: " & targetName & " (" & evtCount & ") -> " & outPath

            -- Batch fetch titles and dates (fast: 2 round trips total)
            set allTitles to summary of every event of targetCal
            set allDates to start date of every event of targetCal
            -- Per-event attendee fetch is ~83ms/event; only do it for smaller calendars
            set fetchAttendees to evtCount ≤ 500

            -- Build output list
            set outputLines to {}
            set evts to every event of targetCal
            repeat with i from 1 to evtCount
                try
                    set t to item i of allTitles
                    if t is missing value then set t to "(No Title)"
                    set sdStr to (item i of allDates) as string

                    set attList to ""
                    if fetchAttendees then
                        try
                            set atts to attendees of item i of evts
                            if (count of atts) > 0 then
                                repeat with a in atts
                                    set aName to display name of a
                                    if aName is not missing value and aName is not "" then
                                        if attList is "" then
                                            set attList to aName
                                        else
                                            set attList to attList & "|" & aName
                                        end if
                                    end if
                                end repeat
                            end if
                        end try
                    end if

                    set end of outputLines to "TITLE: " & t
                    set end of outputLines to "START: " & sdStr
                    if attList is not "" then
                        set end of outputLines to "ATTENDEES: " & attList
                    end if
                    set end of outputLines to "---"
                end try
            end repeat

            -- Join and write
            set AppleScript's text item delimiters to linefeed
            set output to (outputLines as text) & linefeed
            set AppleScript's text item delimiters to ""

            set outFile to open for access POSIX file outPath with write permission
            set eof of outFile to 0
            write output to outFile
            close access outFile

            set end of logLines to "DONE: " & outPath
        end if
    end repeat

end tell

-- Write log
set AppleScript's text item delimiters to linefeed
set logOutput to (logLines as text) & linefeed
set AppleScript's text item delimiters to ""
set logFile to open for access POSIX file "/tmp/cal_export_log.txt" with write permission
set eof of logFile to 0
write logOutput to logFile
close access logFile
