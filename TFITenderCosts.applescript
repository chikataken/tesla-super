-- Mac helper for the v6-sort Excel macro's tender-cost lookup.
-- Mac Excel VBA cannot make HTTP calls natively, so the macro shells out to
-- curl through this script via AppleScriptTask. ONE-TIME INSTALL per Mac:
-- copy this file (exact name) into:
--   ~/Library/Application Scripts/com.microsoft.Excel/
-- (that precise folder is an Apple sandbox requirement - nothing else works).
--
-- Returns the response body followed by "\n<http status>"; "CURLERR" if curl
-- itself failed (no network, DNS, timeout).
on FetchCosts(theUrl)
    try
        return do shell script "curl -s -m 15 -w '\\n%{http_code}' " & quoted form of theUrl
    on error
        return "CURLERR"
    end try
end FetchCosts
