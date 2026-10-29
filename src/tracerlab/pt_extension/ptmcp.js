// PT-MCP script module (Script Engine file), exported as ptmcp.pts.
// To rebuild: New PT Script Module, add this as main.js and bridge.html as a
// custom interface, set startup to On Startup with all privileges, export.

var ptmcpWindow = null;
var ptmcpWindowId = "";
var ptmcpMenuUuid = "";

// returns JSON: {ok, value} or {ok: false, error, line}
function ptmcpExec(code) {
    try {
        var fn = new Function(code);
        var value = fn();
        return JSON.stringify({ ok: true, value: value === undefined ? null : value });
    } catch (e) {
        return JSON.stringify({ ok: false, error: String(e && e.message ? e.message : e),
                                line: e && e.lineNumber ? e.lineNumber : null });
    }
}

function ptmcpShow() {
    if (ptmcpWindowId === "" || webViewManager.getWebView(ptmcpWindowId) == null) {
        ptmcpWindow = webViewManager.createWebView("PT-MCP Bridge", "this-sm:bridge.html", 380, 190);
        ptmcpWindowId = ptmcpWindow.getWebViewId();
        ptmcpWindow.registerEvent("closed", null, ptmcpClosed);
    }
    ptmcpWindow.show();
}

function ptmcpClosed(src, args) {
    ptmcpWindowId = "";
    ptmcpWindow = null;
}

function ptmcpMenuClicked(src, args) {
    ptmcpShow();
}

function main() {
    try {
        var menu = ipc.appWindow().getMenuBar().getExtensionsPopupMenu();
        ptmcpMenuUuid = menu.insertItem("", "PT-MCP Bridge");
        menu.getMenuItemByUuid(ptmcpMenuUuid).registerEvent("onClicked", null, ptmcpMenuClicked);
    } catch (e) {
        dprint("PT-MCP: could not add menu item: " + e);
    }
    ptmcpShow();
}

function cleanUp() {
    try {
        if (ptmcpMenuUuid !== "") {
            ipc.appWindow().getMenuBar().getExtensionsPopupMenu().removeItemUuid(ptmcpMenuUuid);
            ptmcpMenuUuid = "";
        }
        if (ptmcpWindowId !== "") { webViewManager.closeWebView(ptmcpWindowId); }
    } catch (e) {}
}
