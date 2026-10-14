import Cocoa

let accent = NSColor(calibratedRed: 0.35, green: 0.32, blue: 0.84, alpha: 1)
let ink = NSColor(calibratedWhite: 0.12, alpha: 1)
let muted = NSColor(calibratedWhite: 0.47, alpha: 1)
let lineColor = NSColor(calibratedWhite: 0.90, alpha: 1)

func label(_ text: String, _ size: CGFloat = 13, _ weight: NSFont.Weight = .regular, _ color: NSColor = ink) -> NSTextField {
    let v = NSTextField(labelWithString: text)
    v.font = .systemFont(ofSize: size, weight: weight)
    v.textColor = color
    v.lineBreakMode = .byTruncatingTail
    return v
}
func box(_ color: NSColor, radius: CGFloat = 0) -> NSView {
    let v = NSView(); v.wantsLayer = true; v.layer?.backgroundColor = color.cgColor; v.layer?.cornerRadius = radius
    return v
}
func button(_ text: String, _ target: AnyObject, _ selector: Selector) -> NSButton {
    let b = NSButton(title: text, target: target, action: selector)
    b.bezelStyle = .rounded; b.font = .systemFont(ofSize: 13, weight: .medium)
    return b
}
func timeText(_ value: Any?) -> String {
    guard let n = (value as? NSNumber)?.doubleValue else { return "—" }
    let f = DateFormatter(); f.dateFormat = "MM-dd HH:mm"; f.timeZone = .current
    return f.string(from: Date(timeIntervalSince1970: n))
}
func statusText(_ value: String) -> String {
    return ["active":L("执行中"), "idle":L("已停止"), "notLoaded":L("未加载"), "waiting":L("等待恢复"), "running":L("已续接"), "done":L("续接轮次结束"), "goal_restored":L("Goal 恢复已确认"), "stale_record":L("历史恢复待清理"), "observing":L("监控中"), "unselected":L("未监控"), "filtered":L("已筛选"), "manual_goal":L("需在 Codex 恢复"), "unknown":L("未知"), "none":L("无 Goal"), "complete":L("目标完成"), "needs_attention":L("需检查"), "superseded":L("已取消恢复"), "disabled":L("已移出"), "paused":L("已暂停"), "completed":L("本轮已结束"), "interrupted":L("已停止"), "usageLimited":L("额度受限"), "budgetLimited":L("目标预算已用尽"), "blocked":L("等待处理"), "failed":L("执行失败"), "unavailable":L("待连接"), "inProgress":L("执行中")][value] ?? value
}

func scheduleModeText(_ mode:String) -> String {
    let english:[String:String] = ["running":"Running checks","reset_wait":"Waiting for reset","remaining_wait":"Waiting for threshold","quota_wait":"Waiting for quota","manual_pause":"Manual pause: status only","idle":"Idle observation","connection_wait":"Connection backoff","monitor_paused":"Monitoring paused"]
    let chinese:[String:String] = ["running":"运行检查","reset_wait":"等待刷新时间","remaining_wait":"等待恢复阈值","quota_wait":"等待额度恢复","manual_pause":"人工暂停：仅检查状态","idle":"低频状态观察","connection_wait":"连接退避","monitor_paused":"监控已暂停"]
    return (RelayLanguage.current == "en" ? english : chinese)[mode] ?? mode
}

final class QuotaBar: NSView {
    var doubleValue: Double = 0 { didSet { needsDisplay = true; setAccessibilityLabel(L("已使用 \(Int(doubleValue))%")) } }
    override func draw(_ dirtyRect:NSRect) {
        NSColor(calibratedWhite:0.90,alpha:1).setFill()
        NSBezierPath(roundedRect:bounds,xRadius:4,yRadius:4).fill()
        accent.setFill()
        let amount = bounds.width * CGFloat(max(0,min(100,doubleValue))/100)
        if amount > 0 { NSBezierPath(roundedRect:NSRect(x:0,y:0,width:amount,height:bounds.height),xRadius:4,yRadius:4).fill() }
    }
}

final class Backend {
    var process: Process?
    var lastMockAction = "", lastMockParams: [String:Any] = [:]
    var input: FileHandle?
    var output: FileHandle?
    var pending: [Int: (Bool, [String:Any], String) -> Void] = [:]
    var serial = 0
    var buffer = Data()
    var streamID = UUID()
    let io = DispatchQueue(label: "relay.backend.output")
    var onExit: (() -> Void)?
    var mock = false
    var fixture: [String:Any] = [:]

    func start() throws {
        if process?.isRunning == true { return }
        let resources = Bundle.main.resourceURL!
        let configURL = resources.appendingPathComponent("runtime.json")
        let config = (try? JSONSerialization.jsonObject(with: Data(contentsOf: configURL))) as? [String:Any] ?? [:]
        let paths = [config["python"] as? String ?? "", "/usr/local/bin/python3", "/opt/homebrew/bin/python3"]
        guard let python = paths.first(where: { !$0.isEmpty && FileManager.default.isExecutableFile(atPath: $0) }) else {
            throw NSError(domain:"Relay", code:1, userInfo:[NSLocalizedDescriptionKey:L("需要 Python 3.11 或更高版本，请先安装后再打开应用。")])
        }
        let p = Process(), stdin = Pipe(), stdout = Pipe()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-B", "-u", "-m", "codex_autoresume.gui_service"]
        p.currentDirectoryURL = resources
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = resources.path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["CODEX_RELAY_NATIVE_HELPER"] = Bundle.main.executableURL?.path
        env["PATH"] = [NSHomeDirectory()+"/.local/bin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", env["PATH"] ?? ""].joined(separator: ":")
        if let binary = config["codex"] as? String, FileManager.default.isExecutableFile(atPath: binary) { env["CODEX_BINARY"] = binary }
        p.environment = env; p.standardInput = stdin; p.standardOutput = stdout
        let logs = URL(fileURLWithPath: NSHomeDirectory()+"/Library/Logs/CodexRelay")
        try? FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true)
        let logPath = logs.appendingPathComponent("backend.log").path
        if !FileManager.default.fileExists(atPath: logPath) { FileManager.default.createFile(atPath:logPath, contents:nil, attributes:[.posixPermissions:0o600]) }
        if let log = FileHandle(forWritingAtPath:logPath) { log.seekToEndOfFile(); p.standardError = log }
        let stream = UUID(); io.sync { self.buffer.removeAll(); self.streamID = stream }
        input = stdin.fileHandleForWriting; output = stdout.fileHandleForReading
        output?.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            self?.io.async {
                guard let self = self, self.streamID == stream else { return }
                self.buffer.append(data)
                while let end = self.buffer.firstIndex(of:10) {
                    let row = self.buffer.subdata(in:0..<end); self.buffer.removeSubrange(0...end)
                    guard let value = (try? JSONSerialization.jsonObject(with:row)) as? [String:Any], let id = value["id"] as? Int else { continue }
                    DispatchQueue.main.async {
                        self.pending.removeValue(forKey:id)?(value["ok"] as? Bool ?? false, value["result"] as? [String:Any] ?? [:], value["error"] as? String ?? "")
                    }
                }
            }
        }
        p.terminationHandler = { [weak self] terminated in DispatchQueue.main.async {
            guard let self = self, self.process === terminated else { return }
            self.output?.readabilityHandler = nil
            let callbacks = self.pending; self.pending.removeAll()
            callbacks.values.forEach { $0(false, [:], L("后台连接已断开，请点击刷新重新连接。")) }
            self.onExit?()
        }}
        try p.run(); process = p
    }
    func request(_ action:String, _ params:[String:Any] = [:], _ completion:@escaping (Bool,[String:Any],String)->Void) {
        if mock {
            lastMockAction = action; lastMockParams = params
            if action == "goal_control" { fixture["goal_control"] = ["supported":true,"enabled":params["enabled"] as? Bool ?? false,"trusted":true,"ready":params["enabled"] as? Bool ?? false,"reason":L("测试能力状态")] }
            if action == "detail" { completion(true,["status":"idle", "turn_status":"completed", "model":"gpt-6-luna"],""); return }
            if action == "polling" { fixture["polling"] = params }
            if action == "monitor" { fixture["monitoring"] = params["enabled"] as? Bool ?? false }
            if action == "watch", let tid = params["thread_id"] as? String {
                var threads = fixture["threads"] as? [[String:Any]] ?? []
                if let index = threads.firstIndex(where:{$0["id"] as? String == tid}) { threads[index]["selected"] = params["enabled"] as? Bool ?? false }
                fixture["threads"] = threads; fixture["watched_count"] = threads.filter{$0["selected"] as? Bool == true}.count
            }
            if action == "policy", let tid = params["thread_id"] as? String {
                var threads = fixture["threads"] as? [[String:Any]] ?? []
                if let index = threads.firstIndex(where:{$0["id"] as? String == tid}) {
                    let policy:[String:Any] = ["goal_only":(params["goal_only"] as? Bool ?? false), "mode":(params["mode"] as? String ?? "quota"), "stop_remaining":(params["stop_remaining"] as? Double ?? 20), "resume_mode":(params["resume_mode"] as? String ?? "reset"), "resume_remaining":(params["resume_remaining"] as? Double ?? 50), "window":(params["window"] as? String ?? "primary")]
                    threads[index]["policy"] = policy
                }
                fixture["threads"] = threads
            }
            completion(true,fixture,""); return
        }
        do {
            try start(); serial += 1; let id = serial
            pending[id] = completion
            var data = try JSONSerialization.data(withJSONObject:["id":id,"action":action,"params":params]); data.append(10)
            input?.write(data)
            DispatchQueue.main.asyncAfter(deadline:.now()+100) { [weak self] in
                self?.pending.removeValue(forKey:id)?(false,[:],L("操作等待超时，请刷新状态后再检查；未自动重发。"))
            }
        } catch { completion(false,[:],error.localizedDescription) }
    }
    func close() { input?.closeFile(); input = nil }
}

final class RelayApp: NSObject, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSTextFieldDelegate, NSSearchFieldDelegate {
    let backend = Backend()
    var window: NSWindow!
    var root: NSView!
    var statusItem: NSStatusItem!
    var menu: NSMenu!
    var menuStatus: NSMenuItem!, menuQuota: NSMenuItem!, menuToggle: NSMenuItem!
    var connection: NSTextField!, subtitle: NSTextField!, footer: NSTextField!
    var monitorButton: NSButton!, refreshButton: NSButton!, goalControlButton: NSButton!
    var search: NSSearchField!, table: NSTableView!
    var quotaTitle: [NSTextField] = [], quotaValue: [NSTextField] = [], quotaReset: [NSTextField] = [], quotaBars: [QuotaBar] = []
    var inspectorTitle: NSTextField!, inspectorMeta: NSTextField!, detailStatus: NSTextField!
    var goalStatus: NSTextField!, relayStatus: NSTextField!, observationReason: NSTextField!, goalCapability: NSTextField!
    var openCodexButton: NSButton!, evidenceButton: NSButton!
    var currentObservation: [String:Any] = [:]
    var watchButton: NSButton!, goalOnly: NSButton!, strategyChoice: NSPopUpButton!, windowChoice: NSPopUpButton!, recoveryChoice: NSPopUpButton!
    var strategyHint: NSTextField!, reserveViews: [NSView] = [], resumeViews: [NSView] = []
    var pauseField: NSTextField!, resumeField: NSTextField!, saveButton: NSButton!, notice: NSTextField!
    var eventText: NSTextView!
    var allThreads: [[String:Any]] = [], visibleThreads: [[String:Any]] = []
    var currentID: String?, state: [String:Any] = [:]
    var monitoring = false, connected = false, busy = false, dirty = false
    var applyingCachedState = false
    var currentGoalStatus = "unknown", currentGoalControl: Bool?, currentGoalPauseControl: Bool?
    var timer: Timer?
    var demoStatePath: String?
    var demoStateData: Data?
    var previewPath: String?
    var capturePath: String?
    var uiTestPath: String?
    var emptyLabel: NSTextField!
    var restoringSelection = false
    var quitAfterCapture = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        RelayLanguage.configure(args)
        if let index = args.firstIndex(of:"--render-preview"), args.count > index+1 { previewPath = args[index+1]; backend.mock = true; backend.fixture = demoState() }
        if let index = args.firstIndex(of:"--ui-test"), args.count > index+1 { uiTestPath = args[index+1]; backend.mock = true; backend.fixture = demoState() }
        if let index = args.firstIndex(of:"--capture"), args.count > index+1 { capturePath = args[index+1] }
        if let i = args.firstIndex(of:"--demo-state"), args.count > i+1 {
            demoStatePath = args[i+1]; backend.mock = true
            if let data = try? Data(contentsOf:URL(fileURLWithPath:args[i+1])), let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] { backend.fixture = value }
        }
        if backend.mock && args.contains("--preview-scheduler") {
            let now = Date().timeIntervalSince1970
            var rows = backend.fixture["threads"] as? [[String:Any]] ?? []
            rows[0]["observation"] = ["turn_status":"interrupted","goal_status":"paused","relay_status":"waiting","goal_control":true,"goal_pause_control":true,"reason":"Relay confirmed a reserve pause. Waiting for the saved reset time.","checked_at":now-5,"schedule":["mode":"reset_wait","next_check_at":now+3600,"reason":"Waiting for saved reset; early probes disabled"]]
            rows[0]["policy"] = ["mode":"reserve","stop_remaining":20.0,"resume_mode":"reset","resume_remaining":50.0,"goal_only":true,"window":"primary"]
            backend.fixture["threads"] = rows
            backend.fixture["monitoring"] = true
            backend.fixture["waiting_count"] = 1
            backend.fixture["quotas"] = ["rateLimits":["primary":["usedPercent":84.0,"resetsAt":now+3600],"secondary":["usedPercent":65.0,"resetsAt":now+604800]]]
            backend.fixture["goal_control"] = ["enabled":true,"supported":true,"trusted":true,"ready":true]
            backend.fixture["schedule"] = ["mode":"reset_wait","next_check_at":now+3600]
            backend.fixture["polling"] = ["check_seconds":30,"ui_seconds":60,"quota_wait_seconds":300,"early_reset_checks":false]
            backend.fixture["last_cycle"] = now-5
            backend.fixture["quota_checked_at"] = now-5
            backend.fixture["next_check_at"] = now+3600
        }
        quitAfterCapture = args.contains("--capture-and-exit")
        buildMenu(); buildWindow()
        backend.onExit = { [weak self] in self?.setConnection(false, L("后台已退出，点击刷新重新连接")) }
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true)
        if let path = demoStatePath {
            window.title = "Codex Relay — Controlled Demo"
            subtitle.stringValue = "Verified test replay · controlled quota readings"
            apply(backend.fixture)
            timer = Timer.scheduledTimer(withTimeInterval:1,repeats:true) { [weak self] _ in
                guard let self = self, let data = try? Data(contentsOf:URL(fileURLWithPath:path)), let value = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] else { return }
                guard self.demoStateData != data else { return }; self.demoStateData = data
                self.apply(value)
                for control in [self.monitorButton,self.goalControlButton,self.saveButton,self.refreshButton,self.watchButton,self.goalOnly,self.openCodexButton] { control?.isEnabled = false }
            }
            return
        }
        if backend.mock { apply(backend.fixture); if uiTestPath != nil { runUITest() } else { selectRow(0); renderPreview() }; return }
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval:60, repeats:true) { [weak self] _ in
            guard let self = self, !self.busy else { return }; self.refreshState(force:false)
        }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender:NSApplication)->Bool { return false }
    func applicationShouldHandleReopen(_ sender:NSApplication, hasVisibleWindows flag:Bool)->Bool { showWindow(); return true }
    func applicationWillTerminate(_ notification:Notification) { timer?.invalidate(); backend.close() }

    func buildMenu() {
        let main = NSMenu(), app = NSMenu(), item = NSMenuItem()
        app.addItem(withTitle:L("关于 Codex Relay"), action:#selector(about), keyEquivalent:"").target = self
        app.addItem(withTitle:RelayLanguage.current == "en" ? "Monitoring Settings…" : "监控频率设置…",action:#selector(configurePolling),keyEquivalent:",").target = self
        app.addItem(NSMenuItem.separator())
        app.addItem(withTitle:L("退出 Codex Relay"), action:#selector(quit), keyEquivalent:"q").target = self
        item.submenu = app; main.addItem(item)
        let languageItem = NSMenuItem(title:"Language / 语言", action:nil, keyEquivalent:"")
        let languages = NSMenu(title:"Language / 语言")
        languages.addItem(withTitle:"English",action:#selector(useEnglish),keyEquivalent:"").target = self
        languages.addItem(withTitle:"简体中文",action:#selector(useChinese),keyEquivalent:"").target = self
        languageItem.submenu = languages; main.addItem(languageItem)
        let editItem = NSMenuItem(); editItem.title = L("编辑"); let edit = NSMenu(title:L("编辑"))
        edit.addItem(withTitle:L("复制"), action:#selector(NSText.copy(_:)), keyEquivalent:"c")
        edit.addItem(withTitle:L("粘贴"), action:#selector(NSText.paste(_:)), keyEquivalent:"v")
        edit.addItem(withTitle:L("全选"), action:#selector(NSText.selectAll(_:)), keyEquivalent:"a")
        editItem.submenu = edit; main.addItem(editItem); NSApp.mainMenu = main
        statusItem = NSStatusBar.system.statusItem(withLength:NSStatusItem.variableLength)
        statusItem.button?.title = "↻ Relay"
        statusItem.button?.setAccessibilityLabel(L("Codex Relay 菜单栏"))
        menu = NSMenu()
        menuStatus = NSMenuItem(title:L("正在连接…"), action:nil, keyEquivalent:""); menu.addItem(menuStatus)
        menuQuota = NSMenuItem(title:L("额度尚未读取"), action:nil, keyEquivalent:""); menu.addItem(menuQuota)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle:L("打开控制台"), action:#selector(showWindow), keyEquivalent:"").target = self
        menuToggle = NSMenuItem(title:L("开始监控所选会话"), action:#selector(toggleMonitor), keyEquivalent:""); menuToggle.target = self; menu.addItem(menuToggle)
        menu.addItem(withTitle:L("刷新额度与会话"), action:#selector(refresh), keyEquivalent:"").target = self
        menu.addItem(withTitle:RelayLanguage.current == "en" ? "Monitoring Settings…" : "监控频率设置…",action:#selector(configurePolling),keyEquivalent:"").target = self
        menu.addItem(NSMenuItem.separator())
        menu.addItem(withTitle:L("退出 Codex Relay"), action:#selector(quit), keyEquivalent:"").target = self
        statusItem.menu = menu
    }
    @objc func useEnglish() { chooseLanguage("en") }
    @objc func useChinese() { chooseLanguage("zh") }
    func chooseLanguage(_ language:String) {
        UserDefaults.standard.set(language,forKey:"RelayLanguage")
        let alert = NSAlert()
        alert.messageText = language == "en" ? "English selected" : "已选择简体中文"
        alert.informativeText = language == "en" ? "Quit and reopen Relay to apply. Monitoring continues until you quit. Save any edited settings first." : "退出并重新打开 Relay 后生效。退出前监控继续运行，请先保存已编辑的设置。"
        alert.addButton(withTitle:language == "en" ? "OK" : "好")
        alert.beginSheetModal(for:window,completionHandler:nil)
    }
    func buildWindow() {
        window = NSWindow(contentRect:NSRect(x:0,y:0,width:1080,height:900), styleMask:[.titled,.closable,.miniaturizable], backing:.buffered, defer:false)
        window.title = "Codex Relay"; window.center(); window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named:.aqua)
        root = box(.white); window.contentView = root
        let brand = box(accent, radius:11); brand.frame = NSRect(x:28,y:675,width:44,height:44); root.addSubview(brand)
        let mark = label("↻",31,.medium,.white); mark.alignment = .center; mark.frame = NSRect(x:0,y:3,width:44,height:39); brand.addSubview(mark)
        let title = label("Codex Relay",26,.semibold); title.frame = NSRect(x:85,y:690,width:300,height:34); root.addSubview(title)
        subtitle = label(L("续航接力 · 让任务在额度恢复后继续"),12,.regular,muted); subtitle.frame = NSRect(x:86,y:670,width:400,height:20); root.addSubview(subtitle)
        connection = label(L("● 正在连接 Codex…"),12,.medium,muted); connection.frame = NSRect(x:650,y:701,width:200,height:20); root.addSubview(connection)
        monitorButton = button(L("开始监控所选会话"),self,#selector(toggleMonitor)); monitorButton.frame = NSRect(x:855,y:683,width:197,height:34); root.addSubview(monitorButton)
        goalControlButton = button(L("Goal 控制…"),self,#selector(configureGoalControl)); goalControlButton.frame = NSRect(x:650,y:671,width:197,height:28); root.addSubview(goalControlButton)
        let separator = box(lineColor); separator.frame = NSRect(x:28,y:651,width:1024,height:1); root.addSubview(separator)
        makeQuota(L("五小时额度"),x:28,index:0); makeQuota(L("每周额度"),x:550,index:1)
        let listTitle = label(L("会话"),19,.semibold); listTitle.frame = NSRect(x:28,y:486,width:200,height:28); root.addSubview(listTitle)
        let tip = label(L("勾选需要自动续接的会话"),12,.regular,muted); tip.frame = NSRect(x:28,y:465,width:400,height:20); root.addSubview(tip)
        search = NSSearchField(frame:NSRect(x:330,y:478,width:200,height:29)); search.placeholderString = L("搜索会话名称"); search.delegate = self; root.addSubview(search)
        refreshButton = button(L("刷新"),self,#selector(refresh)); refreshButton.frame = NSRect(x:540,y:477,width:78,height:30); root.addSubview(refreshButton)
        let scroll = NSScrollView(frame:NSRect(x:28,y:205,width:590,height:250)); scroll.hasVerticalScroller = true; scroll.borderType = .noBorder
        table = NSTableView(); table.headerView = nil; table.rowHeight = 61; table.intercellSpacing = NSSize(width:0,height:3)
        table.backgroundColor = .white; table.selectionHighlightStyle = .regular; table.delegate = self; table.dataSource = self
        let check = NSTableColumn(identifier:NSUserInterfaceItemIdentifier("check")); check.width = 34; table.addTableColumn(check)
        let name = NSTableColumn(identifier:NSUserInterfaceItemIdentifier("name")); name.width = 423; table.addTableColumn(name)
        let status = NSTableColumn(identifier:NSUserInterfaceItemIdentifier("status")); status.width = 112; table.addTableColumn(status)
        scroll.documentView = table; root.addSubview(scroll)
        emptyLabel = label(L("打开 Codex 后点击刷新，即可选择会话。"),13,.regular,muted); emptyLabel.alignment = .center; emptyLabel.frame = NSRect(x:40,y:310,width:560,height:40); root.addSubview(emptyLabel)
        let divider = box(lineColor); divider.frame = NSRect(x:640,y:93,width:1,height:421); root.addSubview(divider)
        buildInspector()
        let logLabel = label(L("最近操作"),13,.semibold); logLabel.frame = NSRect(x:28,y:170,width:250,height:22); root.addSubview(logLabel)
        let logScroll = NSScrollView(frame:NSRect(x:28,y:87,width:590,height:78)); logScroll.hasVerticalScroller = true
        eventText = NSTextView(frame:NSRect(x:0,y:0,width:575,height:78)); eventText.isEditable = false; eventText.isSelectable = true; eventText.font = .systemFont(ofSize:11); eventText.textColor = muted; eventText.textContainerInset = NSSize(width:0,height:3); eventText.backgroundColor = .white
        logScroll.documentView = eventText; root.addSubview(logScroll)
        for view in root.subviews { view.frame.origin.y += 140 }
        let bottom = box(NSColor(calibratedWhite:0.97,alpha:1)); bottom.frame = NSRect(x:0,y:0,width:1080,height:62); root.addSubview(bottom)
        footer = label(L("正在读取会话与额度…"),12,.regular,muted); footer.frame = NSRect(x:28,y:20,width:920,height:22); bottom.addSubview(footer)
        let version = label("v0.7.0",11,.medium,muted); version.frame = NSRect(x:1000,y:22,width:55,height:18); bottom.addSubview(version)
        // Keep every setting reachable on smaller displays instead of clipping the enlarged inspector.
        if let screen = window.screen ?? NSScreen.main {
            let available = max(560, min(900, screen.visibleFrame.height - 60))
            if available < 900 {
                let scroll = NSScrollView(frame:NSRect(x:0,y:0,width:1080,height:available))
                scroll.hasVerticalScroller = true; scroll.borderType = .noBorder
                root.frame = NSRect(x:0,y:0,width:1080,height:900)
                window.contentView = scroll; scroll.documentView = root
                window.setContentSize(NSSize(width:1080,height:available))
                scroll.contentView.scroll(to:NSPoint(x:0,y:900-available)); scroll.reflectScrolledClipView(scroll.contentView)
                window.center()
            }
        }
    }
    func makeQuota(_ text:String,x:CGFloat,index:Int) {
        let surface = box(NSColor(calibratedWhite:0.975,alpha:1),radius:12); surface.frame = NSRect(x:x,y:534,width:502,height:94); root.addSubview(surface)
        let t = label(text,13,.medium); t.frame = NSRect(x:18,y:61,width:230,height:19); surface.addSubview(t); quotaTitle.append(t)
        let value = label("—",20,.semibold,accent); value.alignment = .right; value.frame = NSRect(x:285,y:50,width:197,height:33); surface.addSubview(value); quotaValue.append(value)
        let bar = QuotaBar(frame:NSRect(x:18,y:35,width:464,height:8)); surface.addSubview(bar); quotaBars.append(bar)
        let reset = label(L("刷新时间尚未读取"),11,.regular,muted); reset.frame = NSRect(x:18,y:9,width:464,height:18); surface.addSubview(reset); quotaReset.append(reset)
    }
    func buildInspector() {
        inspectorTitle = label(L("选择一个会话"),18,.semibold); inspectorTitle.frame = NSRect(x:665,y:478,width:380,height:28); root.addSubview(inspectorTitle)
        inspectorMeta = label(L("在左侧查看详情并选择监控"),11,.regular,muted); inspectorMeta.frame = NSRect(x:665,y:451,width:380,height:22); root.addSubview(inspectorMeta)
        detailStatus = label("",12,.medium,accent); detailStatus.frame = NSRect(x:665,y:423,width:380,height:23); root.addSubview(detailStatus)
        goalStatus = label("",12,.medium); goalStatus.frame = NSRect(x:665,y:399,width:220,height:23); root.addSubview(goalStatus)
        relayStatus = label("",12,.medium,accent); relayStatus.frame = NSRect(x:665,y:375,width:380,height:23); root.addSubview(relayStatus)
        observationReason = label("",11,.regular,muted); observationReason.frame = NSRect(x:665,y:326,width:375,height:45); observationReason.maximumNumberOfLines = 3; observationReason.lineBreakMode = .byWordWrapping; root.addSubview(observationReason)
        goalCapability = label(L("Goal 控制能力尚未确认"),10,.regular,muted); goalCapability.frame = NSRect(x:665,y:306,width:375,height:17); root.addSubview(goalCapability)
        openCodexButton = button(L("在 Codex 中打开"),self,#selector(openInCodex)); openCodexButton.frame = NSRect(x:887,y:397,width:150,height:26); root.addSubview(openCodexButton)
        evidenceButton = button(L("查看依据"),self,#selector(showEvidence)); evidenceButton.frame = NSRect(x:937,y:373,width:100,height:25); root.addSubview(evidenceButton)
        relayStatus.frame.size.width = 265
        let controlsStart = root.subviews.count
        watchButton = NSButton(checkboxWithTitle:L("监控这个会话"),target:self,action:#selector(inspectorWatch)); watchButton.frame = NSRect(x:662,y:388,width:270,height:26); root.addSubview(watchButton)
        goalOnly = NSButton(checkboxWithTitle:L("仅监控含 Goal 的会话"),target:self,action:#selector(policyChanged)); goalOnly.frame = NSRect(x:662,y:354,width:385,height:26); goalOnly.toolTip = L("仅筛选具有正式 Goal 的会话，不代表 Relay 可控制 Goal。当前 Goal 恢复须在 Codex 中手动操作。"); root.addSubview(goalOnly)
        let separator = box(lineColor); separator.frame = NSRect(x:665,y:337,width:360,height:1); root.addSubview(separator)
        strategyChoice = NSPopUpButton(frame:NSRect(x:665,y:299,width:356,height:28)); strategyChoice.addItems(withTitles:[L("额度耗尽后自动续接"), L("提前保留额度")]); strategyChoice.target = self; strategyChoice.action = #selector(policyChanged); root.addSubview(strategyChoice)
        strategyHint = label("",11,.regular,muted); strategyHint.frame = NSRect(x:665,y:253,width:360,height:40); strategyHint.maximumNumberOfLines = 2; strategyHint.lineBreakMode = .byWordWrapping; root.addSubview(strategyHint)
        windowChoice = NSPopUpButton(frame:NSRect(x:665,y:229,width:356,height:28)); windowChoice.addItems(withTitles:[L("五小时额度窗口"), L("每周额度窗口")]); windowChoice.target = self; windowChoice.action = #selector(policyChanged); root.addSubview(windowChoice); reserveViews.append(windowChoice)
        let stopLabel = label(L("剩余额度 ≤ 此值时暂停"),12,.regular,muted); stopLabel.frame = NSRect(x:665,y:200,width:175,height:19); root.addSubview(stopLabel); reserveViews.append(stopLabel)
        pauseField = NSTextField(string:"20"); pauseField.font = .systemFont(ofSize:17,weight:.medium); pauseField.delegate = self; pauseField.frame = NSRect(x:665,y:168,width:130,height:29); root.addSubview(pauseField); reserveViews.append(pauseField)
        let percent = label("%",14,.medium,muted); percent.frame = NSRect(x:802,y:173,width:22,height:22); root.addSubview(percent); reserveViews.append(percent)
        let continueLabel = label(L("何时自动继续"),12,.regular,muted); continueLabel.frame = NSRect(x:855,y:200,width:160,height:19); root.addSubview(continueLabel); reserveViews.append(continueLabel)
        recoveryChoice = NSPopUpButton(frame:NSRect(x:850,y:168,width:175,height:29)); recoveryChoice.addItems(withTitles:[L("下一次额度刷新后"), L("剩余额度达到指定值")]); recoveryChoice.target = self; recoveryChoice.action = #selector(policyChanged); root.addSubview(recoveryChoice); reserveViews.append(recoveryChoice)
        let resumeLabel = label(L("剩余额度 ≥"),11,.regular,muted); resumeLabel.frame = NSRect(x:855,y:141,width:90,height:19); root.addSubview(resumeLabel); resumeViews.append(resumeLabel)
        resumeField = NSTextField(string:"50"); resumeField.font = .systemFont(ofSize:15,weight:.medium); resumeField.delegate = self; resumeField.frame = NSRect(x:943,y:137,width:58,height:26); root.addSubview(resumeField); resumeViews.append(resumeField)
        let resumePercent = label("%",12,.medium,muted); resumePercent.frame = NSRect(x:1005,y:141,width:20,height:19); root.addSubview(resumePercent); resumeViews.append(resumePercent)
        let note = label(L("按整个账号判断；实际恢复须由服务端确认。"),11,.regular,muted); note.frame = NSRect(x:665,y:114,width:380,height:20); root.addSubview(note)
        saveButton = button(L("保存会话设置"),self,#selector(savePolicy)); saveButton.frame = NSRect(x:660,y:81,width:150,height:32); root.addSubview(saveButton)
        notice = label(L("先勾选会话，再设置策略。"),11,.regular,muted); notice.frame = NSRect(x:665,y:64,width:375,height:17); root.addSubview(notice)
        for view in root.subviews.dropFirst(controlsStart) { view.frame.origin.y -= 110 }
        enableInspector(false)
    }
    func enableInspector(_ selected:Bool) {
        watchButton.isEnabled = currentID != nil && connected && !busy
        goalOnly.isEnabled = selected && !busy; strategyChoice.isEnabled = selected && !busy
        let reserve = strategyChoice.indexOfSelectedItem == 1
        let custom = reserve && recoveryChoice.indexOfSelectedItem == 1
        strategyHint.stringValue = reserve ? L("主动预留额度；暂停后按所选恢复方式续接。") : L("运行至服务端额度限制，确认额度恢复后继续。\n无需设置百分比，也不会提前暂停。")
        if reserve && currentGoalStatus != "none" && currentGoalStatus != "unknown" {
            if currentGoalControl == true && currentGoalPauseControl == true {
                strategyHint.stringValue = L("按当前额度策略暂停 Goal，满足条件后\n通过辅助功能恢复。")
            } else {
                strategyHint.stringValue = currentGoalControl == true ? L("辅助功能恢复已启用；额度受限时尝试恢复。\nGoal 预算自动暂停暂未启用。") : L("当前仅观察 Goal，不会按预算自动暂停或恢复。\n请在 Codex 中操作 Goal。")
            }
        }
        for view in reserveViews { view.isHidden = !reserve }
        for view in resumeViews { view.isHidden = !custom }
        windowChoice.isEnabled = selected && reserve && !busy; pauseField.isEnabled = selected && reserve && !busy
        recoveryChoice.isEnabled = selected && reserve && !busy; resumeField.isEnabled = selected && custom && !busy
        saveButton.isEnabled = selected && !busy && dirty
    }
    @objc func showWindow() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true) }
    @objc func quit() { NSApp.terminate(nil) }
    @objc func about() {
        NSApp.orderFrontStandardAboutPanel(options:[.applicationName:"Codex Relay", .applicationVersion:"0.7.0", .credits:NSAttributedString(string:L("续航接力\n所选会话的额度恢复与预算管理。\n关闭窗口后仍在菜单栏；退出应用会停止监控。"))])
    }
    func setConnection(_ ok:Bool,_ message:String) {
        connected = ok; connection.stringValue = "● " + message
        connection.textColor = ok ? NSColor(calibratedRed:0.18,green:0.52,blue:0.39,alpha:1) : muted
    }
    @objc func refresh() { refreshState(force:true) }
    func refreshState(force:Bool) {
        guard !busy else { return }
        refreshButton.isEnabled = false
        backend.request("state",["force":force]) { [weak self] ok,result,error in
            guard let self = self else { return }; self.refreshButton.isEnabled = true
            if ok { self.apply(result); if let path = self.capturePath { self.capturePath = nil; self.capture(path) } } else { self.displayError(error) }
        }
    }
    func apply(_ value:[String:Any]) {
        applyingCachedState = value["cached"] as? Bool ?? false
        defer { applyingCachedState = false }
        let polling = value["polling"] as? [String:Any] ?? [:]
        let uiSeconds = (polling["ui_seconds"] as? NSNumber)?.doubleValue ?? 60
        if !backend.mock && (timer == nil || timer!.timeInterval != uiSeconds) {
            timer?.invalidate()
            timer = Timer.scheduledTimer(withTimeInterval:uiSeconds,repeats:true) { [weak self] _ in
                guard let self = self, !self.busy else { return }; self.refreshState(force:false)
            }
        }
        state = value; monitoring = value["monitoring"] as? Bool ?? false
        setConnection(value["connected"] as? Bool ?? false, (value["connected"] as? Bool == true) ? L("已连接 Codex") : L("Codex 未连接"))
        let capability = value["goal_control"] as? [String:Any] ?? [:]
        goalControlButton.title = goalControlTitle(capability)
        goalControlButton.toolTip = (capability["reason"] as? String).map(L) ?? L("默认关闭；仅在明确启用后申请辅助功能权限。")
        goalControlButton.isEnabled = !busy
        let count = value["watched_count"] as? Int ?? 0
        monitorButton.title = monitoring ? L("暂停自动监控") : L("开始监控所选会话")
        let external = value["external_monitor"] as? Bool ?? false
        if external { monitorButton.title = L("其他实例监控中") }
        monitorButton.isEnabled = !busy && !external && (monitoring || (connected && count > 0))
        menuToggle.title = monitoring ? L("暂停自动监控") : L("开始监控所选会话")
        menuToggle.isEnabled = monitorButton.isEnabled
        statusItem.button?.title = monitoring ? (connected ? "↻ Relay ●" : "↻ Relay !") : "↻ Relay"
        let waiting = value["waiting_count"] as? Int ?? 0
        let unavailable = value["unavailable_count"] as? Int ?? 0
        let attention = value["attention_count"] as? Int ?? 0
        menuStatus.title = monitoring ? (connected ? L("监控中 · \(count) 个会话 · 等待恢复 \(waiting)") : L("等待 Codex 连接 · 已选 \(count) 个会话")) : L("监控已暂停 · 已选 \(count) 个会话")
        menuStatus.title += L(" · 需检查 \(attention)")
        let quotas = value["quotas"] as? [String:Any] ?? [:]
        let buckets = quotas["rateLimitsByLimitId"] as? [String:[String:Any]] ?? [:]
        let snapshot = buckets["codex"] ?? (quotas["rateLimits"] as? [String:Any] ?? [:])
        for (index,key) in ["primary","secondary"].enumerated() {
            let q = snapshot[key] as? [String:Any] ?? [:]
            if let used = q["usedPercent"] as? Double {
                let remaining = max(0,100-used); quotaValue[index].stringValue = String(format:L("%.0f%% 剩余"),remaining); quotaBars[index].doubleValue = used
                quotaReset[index].stringValue = L("已用 \(Int(used))% · 刷新于 \(timeText(q["resetsAt"]))")
            } else { quotaValue[index].stringValue = "—"; quotaBars[index].doubleValue = 0; quotaReset[index].stringValue = L("暂时无法读取额度") }
            quotaReset[index].toolTip = (RelayLanguage.current == "en" ? "Quota actually checked: " : "额度实际读取时间：") + timeText(value["quota_checked_at"])
            if let minutes = q["windowDurationMins"] as? Int {
                if minutes >= 1440 { quotaTitle[index].stringValue = L("\(minutes/1440) 天额度") } else { quotaTitle[index].stringValue = L("\(minutes/60) 小时额度") }
            }
        }
        menuQuota.title = L("额度 · 五小时 \(quotaValue[0].stringValue) / 每周 \(quotaValue[1].stringValue)")
        allThreads = value["threads"] as? [[String:Any]] ?? []; filterThreads()
        if let id = currentID, let t = allThreads.first(where:{$0["id"] as? String == id}) { populate(t) }
        let events = value["events"] as? [[String:Any]] ?? []
        eventText.string = events.isEmpty ? L("还没有操作记录。选择会话并开始监控后，恢复与预算操作会记录在这里。") : events.map { e in
            let action = e["action"] as? String ?? ""
            let actionName = ["watch":L("加入监控"), "unwatch":L("移出监控"), "policy":L("保存会话策略"), "pause":L("达到预算，已停止"), "resume":L("已发送继续"), "monitor_start":L("开始监控"), "monitor_stop":L("暂停监控")][action] ?? action
            let tid = e["thread_id"] as? String ?? ""
            let name = self.allThreads.first(where:{$0["id"] as? String == tid})?["name"] as? String ?? String(tid.prefix(8))
            return "\(timeText(e["at"]))   " + ((e["message"] as? String).map(L) ?? "\(actionName) · \(name)")
        }.joined(separator:"\n")
        let scheduler = value["schedule"] as? [String:Any] ?? [:]
        let scheduleMode = scheduleModeText(scheduler["mode"] as? String ?? "")
        if RelayLanguage.current == "en" {
            footer.stringValue = "\(monitoring ? "Monitoring" : "Monitoring paused") · selected \(count) · waiting \(waiting) · \(scheduleMode) · next: \(timeText(value["next_check_at"])) · last successful check: \(timeText(value["last_cycle"]))"
        } else {
            footer.stringValue = "\(monitoring ? "监控中" : "监控已暂停") · 已选 \(count) 个会话 · 等待恢复 \(waiting) · \(scheduleMode) · 下次：\(timeText(value["next_check_at"])) · 上次检查成功：\(timeText(value["last_cycle"]))"
        }
        if let error = value["error"] as? String, !error.isEmpty { footer.stringValue = L(error); footer.textColor = .systemRed } else if external { footer.stringValue = L("另一个 Relay 实例正在监控；请先在原窗口暂停，或在其终端按 Ctrl+C。"); footer.textColor = muted } else if unavailable > 0 { footer.stringValue = L("\(unavailable) 个所选会话尚未连接，请在 Codex 中打开后刷新。"); footer.textColor = muted } else { footer.textColor = muted }
    }
    func filterThreads() {
        let query = search.stringValue.trimmingCharacters(in:.whitespacesAndNewlines).lowercased()
        visibleThreads = allThreads.filter { query.isEmpty || ($0["name"] as? String ?? "").lowercased().contains(query) }
        let existing = currentID.flatMap { id in allThreads.first(where:{$0["id"] as? String == id}) }
        let target = existing ?? allThreads.first(where:{$0["selected"] as? Bool == true})
        restoringSelection = true
        table.reloadData()
        table.deselectAll(nil)
        restoringSelection = false
        emptyLabel.isHidden = !visibleThreads.isEmpty
        emptyLabel.stringValue = query.isEmpty ? L("打开 Codex 后点击刷新，即可选择会话。") : L("没有匹配的会话")
        if let id = target?["id"] as? String { focusThread(id) }
        else { clearInspector() }
    }
    func focusThread(_ id:String) {
        guard let thread = allThreads.first(where:{$0["id"] as? String == id}) else { return }
        restoringSelection = true
        if let row = visibleThreads.firstIndex(where:{$0["id"] as? String == id}) {
            table.selectRowIndexes(IndexSet(integer:row),byExtendingSelection:false)
        } else { table.deselectAll(nil) }
        restoringSelection = false
        displayThread(thread)
    }
    func clearInspector() {
        currentID = nil; dirty = false; currentGoalStatus = "unknown"; currentGoalControl = nil; currentGoalPauseControl = nil; currentObservation = [:]; evidenceButton.isEnabled = false
        inspectorTitle.stringValue = L("选择一个会话")
        inspectorMeta.stringValue = L("点击左侧会话名称或勾选框，设置会自动显示。")
        detailStatus.stringValue = ""; goalStatus.stringValue = ""; relayStatus.stringValue = ""; observationReason.stringValue = ""; goalCapability.stringValue = ""; openCodexButton.isEnabled = false
        watchButton.state = .off; goalOnly.state = .off; strategyChoice.selectItem(at:0); recoveryChoice.selectItem(at:0)
        pauseField.stringValue = "20"; resumeField.stringValue = "50"
        notice.stringValue = L("先选择会话，再设置监控与策略。"); notice.textColor = muted
        enableInspector(false)
    }
    func controlTextDidChange(_ notification:Notification) {
        if notification.object as? NSSearchField === search { filterThreads() } else { policyChanged() }
    }
    func numberOfRows(in tableView:NSTableView)->Int { return visibleThreads.count }
    func tableView(_ tableView:NSTableView, viewFor column:NSTableColumn?, row:Int)->NSView? {
        let t = visibleThreads[row]; let selected = t["selected"] as? Bool ?? false
        if column?.identifier.rawValue == "check" {
            let b = NSButton(checkboxWithTitle:"", target:self, action:#selector(rowWatch(_:))); b.identifier = NSUserInterfaceItemIdentifier(t["id"] as? String ?? ""); b.state = selected ? .on : .off; b.isEnabled = connected && !busy; b.setAccessibilityLabel(L("监控 ") + (t["name"] as? String ?? L("会话"))); return b
        }
        if column?.identifier.rawValue == "status" {
            let job = t["job"] as? [String:Any]
            let observation = t["observation"] as? [String:Any] ?? [:]
            let s = selected ? (observation["relay_status"] as? String ?? job?["status"] as? String ?? t["status"] as? String ?? L("已选择")) : ""
            let l = label(statusText(s),11,.medium,selected ? accent : muted); return l
        }
        let v = NSView(frame:NSRect(x:0,y:0,width:435,height:61))
        let title = label(t["name"] as? String ?? L("未命名会话"),13,.medium); title.frame = NSRect(x:0,y:30,width:425,height:22); v.addSubview(title)
        let path = (t["cwd"] as? String ?? "").replacingOccurrences(of:NSHomeDirectory(),with:"~")
        let sub = label(path,10,.regular,muted); sub.frame = NSRect(x:0,y:9,width:425,height:17); v.addSubview(sub); return v
    }
    func tableViewSelectionDidChange(_ notification:Notification) { if !restoringSelection { selectRow(table.selectedRow) } }
    func selectRow(_ row:Int) {
        guard row >= 0, row < visibleThreads.count else { return }
        displayThread(visibleThreads[row])
    }
    func displayThread(_ t:[String:Any]) {
        let id = t["id"] as? String
        let changed = currentID != id
        if changed { dirty = false; currentID = id }
        populate(t)
        if changed, let id = id, connected && !applyingCachedState {
            detailStatus.stringValue = L("正在读取会话状态…")
            backend.request("detail",["thread_id":id]) { [weak self] ok,result,error in
                guard let self = self, self.currentID == id else { return }
                if ok { self.populateObservation(result) }
                else { self.detailStatus.stringValue = L(error) }
            }
        }
    }
    func populate(_ t:[String:Any]) {
        inspectorTitle.stringValue = t["name"] as? String ?? L("未命名会话")
        inspectorTitle.toolTip = inspectorTitle.stringValue
        inspectorMeta.stringValue = L("会话 ") + String((t["id"] as? String ?? "").prefix(18))
        populateObservation(t)
        let selected = t["selected"] as? Bool ?? false; watchButton.state = selected ? .on : .off
        if !dirty {
            let p = t["policy"] as? [String:Any] ?? [:]
            goalOnly.state = (p["goal_only"] as? Int == 1 || p["goal_only"] as? Bool == true) ? .on : .off
            strategyChoice.selectItem(at:p["mode"] as? String == "reserve" ? 1 : 0)
            recoveryChoice.selectItem(at:p["resume_mode"] as? String == "remaining" ? 1 : 0)
            pauseField.stringValue = String(format:"%g",p["stop_remaining"] as? Double ?? 20)
            resumeField.stringValue = String(format:"%g",p["resume_remaining"] as? Double ?? 50)
            windowChoice.selectItem(at:(p["window"] as? String == "secondary") ? 1 : 0)
        }
        if !dirty { notice.stringValue = selected ? L("已选择此会话；修改后点击保存。") : L("先勾选会话，再设置策略。") }
        enableInspector(selected)
    }
    func populateObservation(_ t:[String:Any]) {
        let o = t["observation"] as? [String:Any] ?? [:]
        currentObservation = o; evidenceButton.isEnabled = !o.isEmpty
        let turn = o["turn_status"] as? String ?? t["turn_status"] as? String ?? "unknown"
        let goal = o["goal_status"] as? String ?? t["goal_status"] as? String ?? "unknown"
        let relay = o["relay_status"] as? String ?? "unknown"
        detailStatus.stringValue = L("会话轮次：") + (turn == "unknown" ? L("尚未确认") : statusText(turn))
        goalStatus.stringValue = L("Goal：") + (goal == "none" ? L("无正式目标") : goal == "unknown" ? L("尚未确认") : statusText(goal))
        relayStatus.stringValue = L("Relay：") + (relay == "unknown" ? L("尚未检查") : statusText(relay))
        let control = o["goal_control"] as? Bool ?? t["goal_control"] as? Bool
        currentGoalStatus = goal; currentGoalControl = control; currentGoalPauseControl = o["goal_pause_control"] as? Bool ?? t["goal_pause_control"] as? Bool
        goalCapability.isHidden = goal == "none"
        if control == false {
            let global = state["goal_control"] as? [String:Any] ?? [:]
            if global["enabled"] as? Bool == false {
                goalCapability.stringValue = (goal == "paused" || goal == "usageLimited") ? L("Goal 自动控制未启用；请在 Codex 中恢复") : L("Goal 自动控制未启用；当前仅观察")
            } else if global["enabled"] as? Bool == true {
                goalCapability.stringValue = L("Goal 控制待授权或验证；当前仅观察")
            } else { goalCapability.stringValue = L("Goal 控制能力尚未确认；当前仅观察") }
        } else if control == true { goalCapability.stringValue = L("Goal 辅助功能控制已就绪") } else { goalCapability.stringValue = L("Goal 控制能力尚未确认") }
        goalCapability.toolTip = currentGoalPauseControl == true ? L("按保存的额度策略暂停 Goal；恢复时切换对应 Codex 窗口并核验恢复按钮。不会删除 Goal，也不会恢复用户手动暂停的目标。") : L("辅助功能仅恢复 Goal：切换对应 Codex 窗口并核验恢复按钮；Goal 预算自动暂停尚未启用。发送普通“继续”不等于恢复 Goal。")
        openCodexButton.isEnabled = codexThreadURL(t["id"] as? String ?? currentID) != nil
        let reason = o["reason"] as? String ?? t["live_error"] as? String ?? L("等待读取状态；会话轮次与 Goal 状态分别确认。")
        let schedule = o["schedule"] as? [String:Any] ?? [:]
        let scheduledLine = schedule.isEmpty ? "" : scheduleModeText(schedule["mode"] as? String ?? "") + (RelayLanguage.current == "en" ? " · next: " : " · 下次：") + timeText(schedule["next_check_at"])
        observationReason.stringValue = L(reason) + (scheduledLine.isEmpty ? "" : "\n" + scheduledLine)
        observationReason.toolTip = L(reason) + "\n" + scheduledLine + "\n" + (schedule["reason"] as? String ?? "") + L("\n状态读取：") + timeText(o["checked_at"])
        for field in [detailStatus!,goalStatus!,relayStatus!] { field.toolTip = field.stringValue }
        enableInspector(watchButton.state == .on)
    }
    func evidenceText() -> String {
        let thread = allThreads.first(where:{$0["id"] as? String == currentID}) ?? [:]
        let payload:[String:Any] = ["thread_id":currentID ?? "", "observation":currentObservation, "policy":thread["policy"] ?? [:]]
        guard JSONSerialization.isValidJSONObject(payload), let data = try? JSONSerialization.data(withJSONObject:payload,options:[.prettyPrinted,.sortedKeys]), let text = String(data:data,encoding:.utf8) else { return L("暂时无法格式化依据。") }
        return text
    }
    @objc func showEvidence() {
        guard !currentObservation.isEmpty else { return }
        let alert = NSAlert(); alert.messageText = L("当前判断依据"); alert.informativeText = L("只读快照：包含来源、观察时间、轮次与 Goal 证据、动作回执及已保存策略。")
        alert.addButton(withTitle:L("关闭"))
        let scroll = NSScrollView(frame:NSRect(x:0,y:0,width:600,height:360)); scroll.hasVerticalScroller = true; scroll.borderType = .bezelBorder
        let text = NSTextView(frame:NSRect(x:0,y:0,width:580,height:360)); text.isEditable = false; text.isSelectable = true; text.font = .monospacedSystemFont(ofSize:11,weight:.regular); text.string = evidenceText(); text.textContainerInset = NSSize(width:8,height:8)
        scroll.documentView = text; alert.accessoryView = scroll
        alert.beginSheetModal(for:window,completionHandler:nil)
    }
    func codexThreadURL(_ id:String?) -> URL? {
        guard let id = id, UUID(uuidString:id) != nil else { return nil }
        return URL(string:"codex://threads/" + id)
    }
    @objc func openInCodex() {
        guard let url = codexThreadURL(currentID) else { return }
        if !NSWorkspace.shared.open(url) { displayError(L("无法打开 Codex，请手动切换到该会话。")) }
    }
    @objc func rowWatch(_ sender:NSButton) {
        guard !busy, let id = sender.identifier?.rawValue else { return }
        focusThread(id)
        mutate("watch",["thread_id":id,"enabled":sender.state == .on])
    }
    @objc func inspectorWatch() {
        guard let id = currentID else { return }; mutate("watch",["thread_id":id,"enabled":watchButton.state == .on])
    }
    @objc func policyChanged() {
        dirty = true; notice.stringValue = L("有未保存的设置"); notice.textColor = accent
        enableInspector(watchButton.state == .on)
    }
    @objc func savePolicy() {
        guard let id = currentID else { return }
        let reserve = strategyChoice.indexOfSelectedItem == 1, custom = recoveryChoice.indexOfSelectedItem == 1
        let pause = Double(pauseField.stringValue) ?? 20, resume = Double(resumeField.stringValue) ?? 50
        if reserve && (Double(pauseField.stringValue) == nil || !pause.isFinite || !(pause >= 0 && pause < 100)) { displayError(L("暂停值须在 0%（含）到 100%（不含）之间。")); return }
        if reserve && custom && (Double(resumeField.stringValue) == nil || !resume.isFinite || !(resume > pause && resume <= 100)) { displayError(L("恢复阈值必须高于暂停值，且不超过 100%。")); return }
        // Hidden drafts must not leak NaN or an invalid range into JSON or persistence.
        let stored = allThreads.first(where:{$0["id"] as? String == id})?["policy"] as? [String:Any] ?? [:]
        let storedPause = stored["stop_remaining"] as? Double ?? 20
        let storedResume = stored["resume_remaining"] as? Double ?? 50
        let safePause = reserve ? pause : (storedPause.isFinite && storedPause >= 0 && storedPause < 100 ? storedPause : 20)
        let safeResume = reserve && custom ? resume : (storedResume.isFinite && storedResume >= 0 && storedResume <= 100 ? storedResume : 50)
        mutate("policy",["thread_id":id,"mode":reserve ? "reserve" : "quota","goal_only":goalOnly.state == .on,"stop_remaining":safePause,"resume_mode":custom ? "remaining" : "reset","resume_remaining":safeResume,"window":windowChoice.indexOfSelectedItem == 1 ? "secondary" : "primary"])
    }
    func goalControlTitle(_ capability:[String:Any]) -> String {
        guard capability["enabled"] as? Bool == true else { return L("Goal 控制：关闭…") }
        let ready = capability["ready"] as? Bool ?? (capability["trusted"] as? Bool == true && capability["supported"] as? Bool == true)
        return ready ? L("Goal 控制：就绪…") : L("Goal 控制：待授权或验证…")
    }
    func setGoalControl(_ enabled:Bool) { mutate("goal_control",["enabled":enabled,"request_permission":enabled]) }
    @objc func configureGoalControl() {
        let alert = NSAlert(); alert.messageText = L("Goal 辅助功能控制")
        alert.informativeText = L("此功能默认关闭，需要在 macOS 辅助功能中授权。启用后，在“提前保留额度”策略下会按阈值暂停 Goal；满足恢复条件时会切换到对应 Codex 窗口，并仅点击核验过的目标恢复按钮。不会删除目标或恢复你手动暂停的 Goal。权限不足或按钮无法确认时停止操作。\n\n") + L((state["goal_control"] as? [String:Any])?["reason"] as? String ?? "Not enabled.")
        alert.addButton(withTitle:L("启用并申请权限")); alert.addButton(withTitle:L("关闭此功能")); alert.addButton(withTitle:L("取消"))
        alert.beginSheetModal(for:window) { [weak self] response in
            if response == .alertFirstButtonReturn { self?.setGoalControl(true) }
            else if response == .alertSecondButtonReturn { self?.setGoalControl(false) }
        }
    }
    @objc func configurePolling() {
        let p = state["polling"] as? [String:Any] ?? [:]
        let alert = NSAlert()
        let english = RelayLanguage.current == "en"
        alert.messageText = english ? "Monitoring Settings" : "监控频率设置"
        alert.informativeText = english ? "Seconds. Saved changes apply to the next check. Waiting sessions use their own schedule. Automatic dashboard refresh reads cache only. A reset time alone never authorizes recovery." : "单位：秒。保存后下次检查生效。等待中的会话按各自时间调度，界面自动刷新仅读缓存。刷新时间本身不代表允许恢复。"
        let content = NSView(frame:NSRect(x:0,y:0,width:440,height:150))
        let specs:[(String,String,String,Int)] = [
            ("check_seconds","Normal checks (10–300s)","常规监控检查（10–300 秒）",30),
            ("ui_seconds","Dashboard refresh (20–300s)","界面刷新（20–300 秒）",60),
            ("quota_wait_seconds","Waiting probes (60–1800s)","等待状态探测（60–1800 秒）",300)]
        var fields:[NSTextField] = []
        for (index,spec) in specs.enumerated() {
            let title = label(english ? spec.1 : spec.2,12)
            title.frame = NSRect(x:0,y:CGFloat(113-index*35),width:320,height:24);content.addSubview(title)
            let field = NSTextField(string:String(p[spec.0] as? Int ?? spec.3))
            field.frame = NSRect(x:330,y:CGFloat(113-index*35),width:90,height:25);content.addSubview(field);fields.append(field)
        }
        let early = NSButton(checkboxWithTitle:english ? "Check for early quota resets" : "低频检查提前重置的额度",target:nil,action:nil)
        early.frame = NSRect(x:0,y:0,width:440,height:26)
        early.state = (p["early_reset_checks"] as? Bool ?? true) ? .on : .off
        early.toolTip = english ? "If off, known reset waits sleep until the saved reset time. Remaining-threshold policies and unknown reset times still need periodic checks." : "关闭后，已知刷新时间的等待直接睡到该时间。按剩余额度阈值恢复或刷新时间未知时，仍需低频检查。"
        content.addSubview(early)
        alert.accessoryView = content
        alert.addButton(withTitle:english ? "Save" : "保存")
        alert.addButton(withTitle:english ? "Cancel" : "取消")
        alert.beginSheetModal(for:window) { [weak self] result in
            guard result == .alertFirstButtonReturn, let self = self else { return }
            var payload:[String:Any] = [:]
            for (index,spec) in specs.enumerated() {
                guard let number = Int(fields[index].stringValue) else { self.displayError(english ? "Enter whole seconds within the displayed ranges." : "请输入范围内的整数秒数。"); return }
                payload[spec.0] = number
            }
            payload["early_reset_checks"] = early.state == .on
            self.mutate("polling",payload)
        }
    }
    @objc func toggleMonitor() { mutate("monitor",["enabled":!monitoring]) }
    func mutate(_ action:String,_ params:[String:Any]) {
        guard !busy else { return }; busy = true; monitorButton.isEnabled = false; saveButton.isEnabled = false
        notice.stringValue = action == "monitor" ? L("正在切换监控状态…") : L("正在保存…"); notice.textColor = muted
        backend.request(action,params) { [weak self] ok,result,error in
            guard let self = self else { return }; self.busy = false
            if ok {
                if action == "watch" || action == "policy" { self.dirty = false }
                self.apply(result)
                if action == "watch" || action == "policy" { self.notice.stringValue = L("设置已保存"); self.notice.textColor = muted }
            }
            else { self.displayError(error); self.refresh() }
        }
    }
    func displayError(_ error:String) {
        notice.stringValue = L(error); notice.toolTip = L(error); notice.textColor = .systemRed
        footer.stringValue = L(error); footer.textColor = .systemRed
    }
    func demoState()->[String:Any] {
        return ["connected":true,"monitoring":false,"goal_control":["enabled":false,"supported":true,"trusted":false,"ready":false],"watched_count":1,"waiting_count":0,"updated_at":Date().timeIntervalSince1970,"last_cycle":1791979000,
                "quotas":["rateLimits":["primary":["usedPercent":16.0,"resetsAt":1791979657],"secondary":["usedPercent":65.0,"resetsAt":1792472802]]],
                "threads":[["id":"preview-test","name":L("Codex Relay 续接测试（界面预览）"),"cwd":"~/Documents/Codex/codex-relay-smoke","selected":true,"observation":["goal_control":false,"turn_status":"completed","goal_status":"usageLimited","relay_status":"needs_attention","reason":L("续接轮次已结束；Goal 仍显示额度受限，尚无新的中断依据。"),"checked_at":1791979000],"policy":["mode":"reserve","stop_remaining":20.0,"resume_mode":"remaining","resume_remaining":50.0,"goal_only":false,"window":"primary"]],
                           ["id":"preview-two","name":L("接口联调与测试"),"cwd":"~/CodeRepo/project","selected":false,"policy":[:]],
                           ["id":"preview-three","name":L("文档整理"),"cwd":"~/Documents/Codex/notes","selected":false,"policy":[:]]],
                "events":[["at":Date().timeIntervalSince1970,"thread_id":"preview-test","action":"policy"]]]
    }
    func runUITest() {
        var checks:[String:Bool] = [:]
        let originalObservation = allThreads.first(where:{$0["id"] as? String == "preview-test"})!
        populateObservation(["observation":["goal_status":"none","goal_control":false]])
        checks["no_goal_hides_capability"] = goalCapability.isHidden
        populateObservation(["observation":["goal_status":"active","goal_control":false]])
        checks["active_goal_observation_wording"] = goalCapability.stringValue == L("Goal 自动控制未启用；当前仅观察")
        enableInspector(true)
        checks["goal_reserve_explains_observation_only"] = strategyHint.stringValue == L("当前仅观察 Goal，不会按预算自动暂停或恢复。\n请在 Codex 中操作 Goal。")
        populateObservation(["observation":["goal_status":"active","goal_control":true,"relay_status":"goal_restored","turn_status":"inProgress"]])
        enableInspector(true)
        checks["goal_resume_preserves_independent_states"] = relayStatus.stringValue == L("Relay：") + statusText("goal_restored") && goalStatus.stringValue == L("Goal：") + statusText("active") && detailStatus.stringValue == L("会话轮次：执行中")
        checks["goal_resume_does_not_enable_budget_pause"] = strategyHint.stringValue.contains(L("预算自动暂停暂未启用"))
        populateObservation(["observation":["goal_status":"active","goal_control":true,"goal_pause_control":true]])
        enableInspector(true)
        checks["goal_budget_capability_dynamic"] = strategyHint.stringValue.contains(L("按当前额度策略暂停 Goal")) && goalCapability.toolTip?.contains(L("不会删除 Goal")) == true
        populateObservation(originalObservation); enableInspector(true)
        var staleThread = originalObservation
        staleThread["observation"] = ["relay_status":"stale_record","goal_status":"usageLimited","reason":L("旧 Goal 中断已处理")]
        staleThread["job"] = ["status":"waiting"]
        let savedRows = visibleThreads; visibleThreads = [staleThread]
        let staleCell = tableView(table,viewFor:table.tableColumns[2],row:0) as! NSTextField
        checks["stale_record_overrides_waiting_label"] = staleCell.stringValue == L("历史恢复待清理")
        visibleThreads = savedRows
        checks["evidence_is_readonly_snapshot"] = evidenceButton.isEnabled && evidenceText().contains("observation") && evidenceText().contains("policy") && evidenceText().contains("preview-test")
        state["goal_control"] = ["enabled":true,"supported":true,"trusted":false]
        populateObservation(originalObservation)
        checks["enabled_untrusted_explains_authorization"] = goalCapability.stringValue == L("Goal 控制待授权或验证；当前仅观察")
        state["goal_control"] = ["enabled":false,"supported":true,"trusted":false]
        populateObservation(originalObservation)
        checks["goal_control_disabled_default"] = goalControlButton.title == goalControlTitle(["enabled":false])
        policyChanged(); setGoalControl(true)
        checks["goal_control_explicit_enable_payload"] = backend.lastMockAction == "goal_control" && backend.lastMockParams["enabled"] as? Bool == true && backend.lastMockParams["request_permission"] as? Bool == true
        checks["goal_control_preserves_dirty"] = dirty
        checks["goal_control_ready_title"] = goalControlButton.title.contains(L("就绪"))
        checks["trusted_without_readiness_not_ready"] = goalControlTitle(["enabled":true,"supported":true,"trusted":true,"ready":false]) == L("Goal 控制：待授权或验证…")
        setGoalControl(false); dirty = false
        checks["goal_capability_explicit"] = goalCapability.stringValue == L("Goal 自动控制未启用；请在 Codex 中恢复")
        checks["navigation_url_validated"] = codexThreadURL("00000000-0000-4000-8000-000000000001")?.absoluteString == "codex://threads/00000000-0000-4000-8000-000000000001" && codexThreadURL("../unsafe") == nil && !openCodexButton.isEnabled
        checks["distinct_observation_states"] = detailStatus.stringValue == L("会话轮次：本轮已结束") && goalStatus.stringValue == L("Goal：") + statusText("usageLimited") && relayStatus.stringValue.contains(L("需检查"))
        checks["observation_reason_visible"] = observationReason.stringValue.contains(L("尚无新的中断依据")) && observationReason.frame.maxY < relayStatus.frame.minY
        checks["monitor_time_uses_cycle"] = footer.stringValue.contains(timeText(1791979000))
        checks["watched_session_focused_on_load"] = currentID == "preview-test" && inspectorTitle.stringValue.contains(L("续接测试")) && strategyChoice.isEnabled
        let otherRow = visibleThreads.firstIndex(where:{$0["id"] as? String == "preview-two"})!
        let listCheck = tableView(table,viewFor:table.tableColumns[0],row:otherRow) as! NSButton
        listCheck.performClick(nil)
        checks["list_checkbox_opens_inspector"] = currentID == "preview-two" && watchButton.state == .on && strategyChoice.isEnabled && inspectorTitle.stringValue == L("接口联调与测试")
        checks["new_session_defaults_to_quota"] = strategyChoice.indexOfSelectedItem == 0 && pauseField.isHidden
        let refreshed = backend.fixture
        apply(refreshed)
        checks["refresh_preserves_focused_session"] = currentID == "preview-two" && table.selectedRow == otherRow && inspectorTitle.stringValue == L("接口联调与测试")
        strategyChoice.selectItem(at:1); pauseField.stringValue = "22"; policyChanged(); apply(refreshed)
        checks["refresh_preserves_unsaved_budget"] = pauseField.stringValue == "22" && dirty
        search.stringValue = L("文档"); filterThreads()
        checks["search_preserves_inspector_target"] = currentID == "preview-two" && inspectorTitle.stringValue == L("接口联调与测试")
        search.stringValue = ""; filterThreads(); focusThread("preview-test")
        // Restore the fixture selection count before the existing start/stop checks.
        backend.request("watch",["thread_id":"preview-two","enabled":false]) { _,result,_ in self.apply(result) }
        checks["save_and_notice_above_footer"] = saveButton.frame.minY >= 62 && notice.frame.minY >= 62
        checks["menu_bar_created"] = statusItem.button != nil && menu.items.count == 9
        checks["quota_values_render"] = quotaValue[0].stringValue.contains("84") && quotaValue[1].stringValue.contains("35")
        search.stringValue = L("接口"); filterThreads(); checks["search_filters"] = visibleThreads.count == 1
        search.stringValue = ""; filterThreads(); selectRow(0)
        watchButton.performClick(nil); checks["unselect_disables_start"] = !monitorButton.isEnabled
        watchButton.performClick(nil); checks["select_enables_start"] = monitorButton.isEnabled
        recoveryChoice.selectItem(at:1); resumeField.stringValue = "10"; policyChanged(); saveButton.performClick(nil)
        checks["invalid_budget_rejected"] = notice.stringValue.contains(L("恢复阈值"))
        resumeField.stringValue = "50"; pauseField.stringValue = "nan"; policyChanged(); saveButton.performClick(nil)
        checks["nonfinite_stop_rejected"] = notice.stringValue.contains(L("暂停值")) && dirty
        pauseField.stringValue = "20"; policyChanged(); saveButton.performClick(nil)
        checks["remaining_recovery_visible"] = !resumeField.isHidden && resumeField.isEnabled
        recoveryChoice.selectItem(at:0); policyChanged()
        checks["reset_hides_percent_recovery"] = resumeField.isHidden && !pauseField.isHidden
        resumeField.stringValue = "nan"; policyChanged(); saveButton.performClick(nil)
        let resetPolicy = (backend.fixture["threads"] as? [[String:Any]])?.first(where:{$0["id"] as? String == "preview-test"})?["policy"] as? [String:Any] ?? [:]
        checks["reset_ignores_invalid_hidden_draft"] = !dirty && JSONSerialization.isValidJSONObject(resetPolicy)
        pauseField.stringValue = "nan"; resumeField.stringValue = "invalid"
        strategyChoice.selectItem(at:0); policyChanged()
        checks["quota_mode_hides_numeric_controls"] = pauseField.isHidden && recoveryChoice.isHidden && resumeField.isHidden
        saveButton.performClick(nil)
        let savedPolicy = (backend.fixture["threads"] as? [[String:Any]])?.first(where:{$0["id"] as? String == "preview-test"})?["policy"] as? [String:Any]
        checks["quota_mode_saved"] = savedPolicy?["mode"] as? String == "quota"
        checks["quota_ignores_invalid_hidden_drafts"] = !dirty && JSONSerialization.isValidJSONObject(savedPolicy ?? [:]) && savedPolicy?["stop_remaining"] as? Double == 20 && savedPolicy?["resume_remaining"] as? Double == 50
        checks["settings_saved"] = !dirty && notice.stringValue == L("设置已保存")
        monitorButton.performClick(nil); checks["start_updates_menu"] = monitoring && menuToggle.title == L("暂停自动监控")
        toggleMonitor(); checks["pause_updates_window"] = !monitoring && monitorButton.title == L("开始监控所选会话")
        window.performClose(nil); checks["close_keeps_menu_bar"] = !window.isVisible && statusItem.button != nil
        showWindow()
        checks["reset_schedule_wording"] = scheduleModeText("reset_wait") == (RelayLanguage.current == "en" ? "Waiting for reset" : "等待刷新时间")
        let scheduled:[String:Any] = ["goal_status":"paused","turn_status":"interrupted","relay_status":"waiting","reason":"Policy pause","schedule":["mode":"reset_wait","next_check_at":1791979000]]
        populateObservation(["observation":scheduled])
        checks["selected_schedule_visible"] = observationReason.stringValue.contains(scheduleModeText("reset_wait")) && observationReason.stringValue.contains(timeText(1791979000))
        mutate("polling",["check_seconds":30,"ui_seconds":60,"quota_wait_seconds":300,"early_reset_checks":false])
        checks["strict_reset_option_roundtrip"] = (state["polling"] as? [String:Any])?["early_reset_checks"] as? Bool == false
        checks["polling_settings_menu_present"] = menu.items.contains { $0.action == #selector(configurePolling) }
        refreshState(force:false)
        checks["automatic_refresh_uses_cache"] = backend.lastMockAction == "state" && backend.lastMockParams["force"] as? Bool == false
        refresh()
        checks["manual_refresh_forces_read"] = backend.lastMockParams["force"] as? Bool == true
        refreshState(force:false)
        currentID = nil
        var cachedFixture = backend.fixture; cachedFixture["cached"] = true
        apply(cachedFixture)
        checks["cached_auto_selection_does_not_read_detail"] = backend.lastMockAction == "state" && currentID != nil
        let result:[String:Any] = ["checks":checks,"passed":checks.values.allSatisfy{$0}]
        if let path = uiTestPath, let data = try? JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys]) { try? data.write(to:URL(fileURLWithPath:path)); print(String(data:data,encoding:.utf8) ?? "") }
        NSApp.terminate(nil)
    }
    func capture(_ path:String) {
        root.layoutSubtreeIfNeeded()
        guard let bitmap = root.bitmapImageRepForCachingDisplay(in:root.bounds) else { return }
        root.cacheDisplay(in:root.bounds,to:bitmap)
        if let data = bitmap.representation(using:.png,properties:[:]) { try? data.write(to:URL(fileURLWithPath:path)) }
        let result:[String:Any] = ["window":true,"menu_items":menu.items.count,"rows":visibleThreads.count,"monitoring":monitoring,"connected":connected]
        if let data = try? JSONSerialization.data(withJSONObject:result), let text = String(data:data,encoding:.utf8) { print(text); fflush(stdout) }
        if quitAfterCapture { NSApp.terminate(nil) }
    }
    func renderPreview() {
        root.layoutSubtreeIfNeeded()
        DispatchQueue.main.asyncAfter(deadline:.now()+0.4) {
            guard let path = self.previewPath, let bitmap = self.root.bitmapImageRepForCachingDisplay(in:self.root.bounds) else { NSApp.terminate(nil); return }
            self.root.cacheDisplay(in:self.root.bounds,to:bitmap)
            if let data = bitmap.representation(using:.png,properties:[:]) { try? data.write(to:URL(fileURLWithPath:path)) }
            let result:[String:Any] = ["window":true,"menu_items":self.menu.items.count,"rows":self.visibleThreads.count,"monitoring":self.monitoring,"preview":true]
            if let data = try? JSONSerialization.data(withJSONObject:result), let text = String(data:data,encoding:.utf8) { print(text) }
            NSApp.terminate(nil)
        }
    }
}

if CommandLine.arguments.contains("--goal-accessibility") {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let request = (try? JSONSerialization.jsonObject(with:data)) as? [String:Any] ?? [:]
    let result = runGoalAccessibilityCommand(request)
    if let output = try? JSONSerialization.data(withJSONObject:result,options:[.sortedKeys]), let text = String(data:output,encoding:.utf8) { print(text) }
    exit(0)
}

if let index = CommandLine.arguments.firstIndex(of:"--render-icon"), CommandLine.arguments.count > index+1 {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:1024,pixelsHigh:1024,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
    NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep:bitmap)
    accent.setFill(); NSBezierPath(roundedRect:NSRect(x:42,y:42,width:940,height:940),xRadius:215,yRadius:215).fill()
    let attrs:[NSAttributedString.Key:Any] = [.font:NSFont.systemFont(ofSize:850,weight:.medium),.foregroundColor:NSColor.white]
    NSString(string:"↻").draw(in:NSRect(x:143,y:67,width:800,height:900),withAttributes:attrs)
    NSGraphicsContext.restoreGraphicsState()
    if let data = bitmap.representation(using:.png,properties:[:]) { try data.write(to:URL(fileURLWithPath:CommandLine.arguments[index+1])) }
    exit(0)
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = RelayApp()
app.delegate = delegate
app.run()
