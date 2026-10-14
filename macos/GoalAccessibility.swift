import Cocoa
import ApplicationServices

// Goal-specific labels must be verified against the installed renderer. Generic play
// controls are deliberately excluded. Unsupported/localized labels fail closed.
private var gaResumeLabels: Set<String> = ["Resume goal", "恢复目标"]
private func gaNormalize(_ value: String) -> String {
    return value.precomposedStringWithCanonicalMapping.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
}
private func gaAttribute(_ node: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(node, name as CFString, &value) == .success else { return nil }
    return value
}
private func gaText(_ node: AXUIElement) -> [String] {
    return [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute, kAXValueAttribute].compactMap { gaAttribute(node, $0) as? String }.map(gaNormalize).filter { !$0.isEmpty }
}
private func gaChildren(_ node: AXUIElement) -> [AXUIElement] {
    return gaAttribute(node, kAXChildrenAttribute) as? [AXUIElement] ?? []
}
private struct GANode {
    let element: AXUIElement
    let parent: Int?
    let role: String
    let text: [String]
    let enabled: Bool
}
private var gaLastWalkDiagnostic: [String: Any] = [:]
private func gaWalk(_ root: AXUIElement) -> ([GANode], Bool) {
    var result: [GANode] = []
    var pending: [(AXUIElement, Int?, Int)] = [(root, nil, 0)]
    let deadline = Date().addingTimeInterval(8)
    var maxDepth = 0
    while let (node, parent, depth) = pending.popLast() {
        maxDepth = max(maxDepth, depth)
        if result.count >= 3500 || Date() > deadline { gaLastWalkDiagnostic = ["reason": result.count >= 3500 ? "node_limit" : "deadline", "max_depth": maxDepth, "node_count": result.count]; return (result, false) }
        let index = result.count
        result.append(GANode(element: node, parent: parent, role: gaAttribute(node, kAXRoleAttribute) as? String ?? "", text: gaText(node), enabled: (gaAttribute(node, kAXEnabledAttribute) as? NSNumber)?.boolValue ?? false))
        let children = gaChildren(node)
        if depth >= 64 && !children.isEmpty { gaLastWalkDiagnostic = ["reason": "depth_limit", "max_depth": maxDepth, "node_count": result.count]; return (result, false) }
        for child in children.reversed() { pending.append((child, index, depth + 1)) }
    }
    gaLastWalkDiagnostic = ["reason": "complete", "max_depth": maxDepth, "node_count": result.count]
    return (result, true)
}
private func gaMatchesObjective(_ text: [String], _ objective: String) -> Bool {
    let wanted = gaNormalize(objective)
    return wanted.count >= 8 && text.contains(wanted)
}
private func gaDurationSeconds(_ text: String) -> Int? {
    let compact = gaNormalize(text).replacingOccurrences(of: " ", with: "")
    guard !compact.isEmpty, let expression = try? NSRegularExpression(pattern: "([0-9]+)(h|m|s|小时|分|秒)") else { return nil }
    let ns = compact as NSString
    let matches = expression.matches(in: compact, range: NSRange(location: 0, length: ns.length))
    var end = 0, previousUnit = 4, total = 0
    for match in matches {
        guard match.range.location == end, let amount = Int(ns.substring(with: match.range(at: 1))) else { return nil }
        let unit = ns.substring(with: match.range(at: 2))
        let rank = ["h": 3, "小时": 3, "m": 2, "分": 2, "s": 1, "秒": 1][unit] ?? 0
        guard rank < previousUnit else { return nil }
        let factor = rank == 3 ? 3600 : rank == 2 ? 60 : 1
        let (scaled, overflow) = amount.multipliedReportingOverflow(by: factor)
        let (sum, sumOverflow) = total.addingReportingOverflow(scaled)
        guard !overflow && !sumOverflow else { return nil }
        total = sum; previousUnit = rank; end = NSMaxRange(match.range)
    }
    return end == ns.length && !matches.isEmpty ? total : nil
}
private func gaSummaryMatches(_ text: String, label: String, objective: String, expectedTime: Int?) -> Bool {
    let prefix = label + " " + gaNormalize(objective)
    guard text.hasPrefix(prefix + " ") else { return false }
    let suffix = String(text.dropFirst(prefix.count + 1))
    guard let time = gaDurationSeconds(suffix) else { return false }
    return expectedTime == nil || time == expectedTime
}
private func gaCandidate(_ nodes: [GANode], _ index: Int, objective: String, status: String = "usageLimited", expectedTime: Int? = nil) -> Bool {
    let candidate = nodes[index]
    guard candidate.role == kAXButtonRole, candidate.enabled,
          candidate.text.contains(where: { gaResumeLabels.contains($0) }) else { return false }
    // Require the actual objective in the same local goal control group, rather
    // than anywhere in the transcript. Never accept title truncation implicitly.
    var parent = candidate.parent
    for _ in 0..<2 {
        guard let p = parent else { break }
        var local: [String] = []
        var localCount = 0
        for (i, node) in nodes.enumerated() {
            var cursor: Int? = i
            for _ in 0..<5 {
                guard let c = cursor else { break }
                if c == p { local.append(contentsOf: node.text); localCount += 1; break }
                cursor = nodes[c].parent
            }
        }
        let statusLabels = status == "paused" ? ["Paused goal", "已暂停的目标"] : ["Goal usage limited", "目标使用受限"]
        let combined = local.contains { text in statusLabels.contains { label in gaSummaryMatches(text, label: label, objective: objective, expectedTime: expectedTime) } }
        if localCount <= 80 && (combined || (gaMatchesObjective(local, objective) && local.contains(where: { statusLabels.contains($0) }) && (expectedTime == nil || local.contains(where: { gaDurationSeconds($0) == expectedTime })))) { return true }
        parent = nodes[p].parent
    }
    return false
}
private func gaNativePageIdentity(role: String, text: [String], url: String, name: String) -> Bool {
    return role == "AXWebArea" && text.contains(gaNormalize(name)) && url == "app://-/index.html"
}
private func gaPageIndex(_ nodes: [GANode], name: String) -> Int? {
    let matches = nodes.indices.filter { i in
        let node = nodes[i]
        return gaNativePageIdentity(role: node.role, text: node.text, url: (gaAttribute(node.element, "AXURL") as? URL)?.absoluteString ?? "", name: name)
    }
    return matches.count == 1 ? matches[0] : nil
}
private func gaDescendant(_ nodes: [GANode], index: Int, ancestor: Int) -> Bool {
    var cursor: Int? = index
    for _ in 0..<70 {
        guard let current = cursor else { return false }
        if current == ancestor { return true }
        if nodes[current].role == "AXWebArea" { return false }
        cursor = nodes[current].parent
    }
    return false
}
private func gaTargetNamed(_ nodes: [GANode], name: String) -> Bool { return gaPageIndex(nodes, name: name) != nil }
private func gaFreshLocalCandidate(_ candidate: AXUIElement, objective: String, status: String, expectedTime: Int? = nil) -> Bool {
    var ancestor = candidate
    for _ in 0..<2 {
        guard let value = gaAttribute(ancestor, kAXParentAttribute), CFGetTypeID(value) == AXUIElementGetTypeID() else { return false }
        ancestor = unsafeBitCast(value, to: AXUIElement.self)
        let (nodes, complete) = gaWalk(ancestor)
        if complete && nodes.count <= 80, let index = nodes.firstIndex(where: { CFEqual($0.element, candidate) }), gaCandidate(nodes, index, objective: objective, status: status, expectedTime: expectedTime) { return true }
    }
    return false
}
private func gaClickBoundsValid(_ rectangle: CGRect, screens: [CGRect]) -> Bool {
    let values = [rectangle.minX, rectangle.minY, rectangle.width, rectangle.height]
    guard values.allSatisfy({ $0.isFinite }), rectangle.width >= 8, rectangle.height >= 8,
          rectangle.width <= 160, rectangle.height <= 160 else { return false }
    return screens.contains { $0.contains(rectangle) }
}
private func gaSafeParkingPoint(window: CGRect, page: CGRect, screens: [CGRect]) -> CGPoint? {
    guard [window.minX, window.minY, window.width, window.height, page.minX, page.minY, page.width, page.height].allSatisfy({ $0.isFinite }),
          window.width >= 300, window.height >= 100, page.width > 0, page.height > 0 else { return nil }
    let point = CGPoint(x: window.midX, y: window.minY + 12)
    guard window.contains(point), !page.contains(point), screens.contains(where: { $0.contains(point) }), point.y < page.minY else { return nil }
    return point
}
private func gaControlBounds(_ node: AXUIElement) -> CGRect? {
    guard let rawPosition = gaAttribute(node, kAXPositionAttribute), CFGetTypeID(rawPosition) == AXValueGetTypeID(),
          let rawSize = gaAttribute(node, kAXSizeAttribute), CFGetTypeID(rawSize) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(unsafeBitCast(rawPosition, to: AXValue.self), .cgPoint, &point),
          AXValueGetValue(unsafeBitCast(rawSize, to: AXValue.self), .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}
private func gaHitReport(_ point: CGPoint, candidate: AXUIElement) -> [String: Any] {
    var hit: AXUIElement?
    let code = AXUIElementCopyElementAtPosition(AXUIElementCreateSystemWide(), Float(point.x), Float(point.y), &hit)
    var report: [String: Any] = ["query_code": code.rawValue, "belongs_to_control": false]
    guard code == .success, var node = hit else { return report }
    var chain: [[String: Any]] = []
    for _ in 0..<6 {
        let equal = CFEqual(node, candidate)
        chain.append(["role": gaAttribute(node, kAXRoleAttribute) as? String ?? "", "resume_label_match": gaText(node).contains(where: { gaResumeLabels.contains($0) }), "equals_candidate": equal])
        if equal { report["belongs_to_control"] = true; break }
        guard let value = gaAttribute(node, kAXParentAttribute), CFGetTypeID(value) == AXUIElementGetTypeID() else { break }
        node = unsafeBitCast(value, to: AXUIElement.self)
    }
    report["ancestor_chain"] = chain
    return report
}
private func gaHitBelongsToControl(_ point: CGPoint, candidate: AXUIElement) -> Bool {
    return gaHitReport(point, candidate: candidate)["belongs_to_control"] as? Bool == true
}
func runGoalAccessibilityCommand(_ request: [String: Any]) -> [String: Any] {
    let action = request["action"] as? String ?? "probe"
    if action == "self_test" {
        let dummy = AXUIElementCreateSystemWide()
        let fixture = [
            GANode(element: dummy, parent: nil, role: "AXGroup", text: [], enabled: true),
            GANode(element: dummy, parent: 0, role: "AXStaticText", text: ["an exact objective", "目标使用受限"], enabled: true),
            GANode(element: dummy, parent: 0, role: kAXButtonRole, text: ["恢复目标"], enabled: true)
        ]
        let nested = [
            GANode(element: dummy, parent: nil, role: "AXWebArea", text: [], enabled: true),
            GANode(element: dummy, parent: 0, role: "AXWebArea", text: [], enabled: true),
            GANode(element: dummy, parent: 1, role: kAXButtonRole, text: ["恢复目标"], enabled: true)
        ]
        var combined = fixture
        combined[1] = GANode(element: dummy, parent: 0, role: kAXButtonRole, text: ["已暂停的目标 an exact objective 1s"], enabled: true)
        var disabled = fixture
        disabled[2] = GANode(element: dummy, parent: 0, role: kAXButtonRole, text: ["恢复目标"], enabled: false)
        var ambiguous = fixture
        ambiguous.append(fixture[2])
        let checks: [String: Bool] = [
            "duration_4802": gaDurationSeconds("1h 20m 2s") == 4802,
            "duration_duplicate_rejected": gaDurationSeconds("1m 2m") == nil,
            "duration_out_of_order_rejected": gaDurationSeconds("2s 1h") == nil,
            "revised_objective_rejected": !gaSummaryMatches("已暂停的目标 an exact objective revised 1s", label: "已暂停的目标", objective: "an exact objective", expectedTime: 1),
            "fake_clock_rejected": !gaSummaryMatches("已暂停的目标 an exact objective 2s", label: "已暂停的目标", objective: "an exact objective", expectedTime: 1),
            "full_clock_accepted": gaSummaryMatches("已暂停的目标 an exact objective 1h 20m 2s", label: "已暂停的目标", objective: "an exact objective", expectedTime: 4802),
            "combined_summary_accepted": gaCandidate(combined, 2, objective: "an exact objective", status: "paused"),
            "combined_wrong_status_rejected": !gaCandidate(combined, 2, objective: "an exact objective", status: "usageLimited"),
            "safe_titlebar_parking": gaSafeParkingPoint(window: CGRect(x: 0, y: 0, width: 1000, height: 800), page: CGRect(x: 0, y: 60, width: 1000, height: 740), screens: [CGRect(x: 0, y: 0, width: 1000, height: 800)]) != nil,
            "parking_inside_webarea_rejected": gaSafeParkingPoint(window: CGRect(x: 0, y: 0, width: 1000, height: 800), page: CGRect(x: 0, y: 0, width: 1000, height: 800), screens: [CGRect(x: 0, y: 0, width: 1000, height: 800)]) == nil,
            "reasonable_control_bounds": gaClickBoundsValid(CGRect(x: 20, y: 20, width: 24, height: 24), screens: [CGRect(x: 0, y: 0, width: 1000, height: 800)]),
            "offscreen_control_rejected": !gaClickBoundsValid(CGRect(x: -50, y: 20, width: 24, height: 24), screens: [CGRect(x: 0, y: 0, width: 1000, height: 800)]),
            "oversized_control_rejected": !gaClickBoundsValid(CGRect(x: 0, y: 0, width: 500, height: 500), screens: [CGRect(x: 0, y: 0, width: 1000, height: 800)]),
            "nonfinite_control_rejected": !gaClickBoundsValid(CGRect(x: CGFloat.infinity, y: 0, width: 24, height: 24), screens: [CGRect(x: 0, y: 0, width: 1000, height: 800)]),
            "nested_webarea_rejected": !gaDescendant(nested, index: 2, ancestor: 0),
            "nearest_native_webarea_accepted": gaDescendant(nested, index: 2, ancestor: 1),
            "native_page_identity": gaNativePageIdentity(role: "AXWebArea", text: ["test"], url: "app://-/index.html", name: "test"),
            "spoof_url_rejected": !gaNativePageIdentity(role: "AXWebArea", text: ["test"], url: "https://example.com", name: "test"),
            "wrong_title_rejected": !gaNativePageIdentity(role: "AXWebArea", text: ["other"], url: "app://-/index.html", name: "test"),
            "other_group_rejected": !gaDescendant(fixture, index: 2, ancestor: 1),
            "wrong_status_rejected": !gaCandidate(fixture, 2, objective: "an exact objective", status: "paused"),
            "disabled_rejected": !gaCandidate(disabled, 2, objective: "an exact objective"),
            "ambiguous_candidates_detected": ambiguous.indices.filter { gaCandidate(ambiguous, $0, objective: "an exact objective") }.count == 2,
            "local_goal_button": gaCandidate(fixture, 2, objective: "an exact objective"),
            "wrong_goal_rejected": !gaCandidate(fixture, 2, objective: "a different objective"),
            "full_objective": gaMatchesObjective(["an exact objective"], "an exact objective"),
            "truncated_rejected": !gaMatchesObjective(["an exact…"], "an exact objective"),
            "generic_play_rejected": !gaResumeLabels.contains("Play"),
            "delete_rejected": !gaResumeLabels.contains("Delete goal"),
            "whitespace_normalized": gaMatchesObjective([gaNormalize("an  exact\nobjective")], "an exact objective")
        ]
        return ["ok": !checks.values.contains(false), "checks": checks]
    }
    let prompt = request["request_permission"] as? Bool ?? false
    let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: prompt] as CFDictionary)
    let bundleURL = URL(fileURLWithPath: "/Applications/ChatGPT.app")
    guard let bundle = Bundle(url: bundleURL), bundle.bundleIdentifier == "com.openai.codex", let executable = bundle.executableURL else {
        return ["supported": false, "trusted": trusted, "clicked": false, "error": "unsupported_codex_installation"]
    }
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: "com.openai.codex").filter { $0.executableURL?.resolvingSymlinksInPath() == executable.resolvingSymlinksInPath() }
    guard apps.count == 1, let app = apps.first else {
        return ["supported": true, "trusted": trusted, "clicked": false, "error": "codex_not_running_or_ambiguous"]
    }
    if action == "probe" { return ["supported": true, "trusted": trusted, "running": true, "source": "accessibility"] }
    guard trusted else { return ["supported": true, "trusted": false, "clicked": false, "error": "accessibility_permission_required"] }
    let root = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetMessagingTimeout(root, 0.3)
    if action == "resume" {
        guard let tid = request["thread_id"] as? String, UUID(uuidString: tid) != nil,
              let objective = request["objective"] as? String, gaNormalize(objective).count >= 8,
              let name = request["name"] as? String, !gaNormalize(name).isEmpty,
              ["usageLimited", "paused"].contains(request["expected_goal_status"] as? String ?? ""),
              let timeNumber = request["expected_time_used_seconds"] as? NSNumber,
              CFGetTypeID(timeNumber) != CFBooleanGetTypeID(), timeNumber.doubleValue.isFinite,
              timeNumber.doubleValue >= 0, timeNumber.doubleValue < Double(Int.max),
              timeNumber.doubleValue.rounded(.towardZero) == timeNumber.doubleValue else {
            return ["clicked": false, "error": "invalid_goal_resume_identity"]
        }
        guard let url = URL(string: "codex://threads/" + tid) else { return ["clicked": false, "error": "invalid_thread_url"] }
        do { try NSWorkspace.shared.open([url], withApplicationAt: bundleURL, options: [], configuration: [:]) }
        catch { return ["clicked": false, "error": "thread_navigation_failed"] }
        app.activate(options: [.activateIgnoringOtherApps])
        let expectedTime = timeNumber.intValue
        let status = request["expected_goal_status"] as? String ?? "usageLimited"
        let navigationDeadline = Date().addingTimeInterval(3)
        var verified: ([GANode], Int, AXUIElement)?
        var failure = "target_thread_not_verified"
        repeat {
            if let value = gaAttribute(root, kAXFocusedWindowAttribute), CFGetTypeID(value) == AXUIElementGetTypeID() {
                let window = unsafeBitCast(value, to: AXUIElement.self)
                let (nodes, complete) = gaWalk(window)
                if !complete { failure = "accessibility_tree_incomplete" }
                else if app.isActive && gaTargetNamed(nodes, name: name) {
                    let page = gaPageIndex(nodes, name: name)!
                    let matches = nodes.indices.filter { gaDescendant(nodes, index: $0, ancestor: page) && gaCandidate(nodes, $0, objective: objective, status: status, expectedTime: expectedTime) }
                    if matches.count > 1 { return ["clicked": false, "error": "ambiguous_goal_resume_control"] }
                    if let index = matches.first { verified = (nodes, index, window); break }
                    failure = "goal_resume_control_not_verified"
                }
            }
            if Date() < navigationDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.15)) }
        } while Date() < navigationDeadline
        guard let (_, _, window) = verified else { return ["clicked": false, "error": failure] }
        // Select a fresh matched element after navigation, rather than retaining
        // an element from a potentially replaced React accessibility subtree.
        let (freshNodes, freshComplete) = gaWalk(window)
        guard freshComplete, app.isActive,
              let finalWindow = gaAttribute(root, kAXFocusedWindowAttribute), CFEqual(finalWindow, window),
              let page = gaPageIndex(freshNodes, name: name) else {
            return ["clicked": false, "error": "goal_identity_changed_before_press"]
        }
        let freshMatches = freshNodes.indices.filter { gaDescendant(freshNodes, index: $0, ancestor: page) && gaCandidate(freshNodes, $0, objective: objective, status: status, expectedTime: expectedTime) }
        guard freshMatches.count == 1 else { return ["clicked": false, "error": "goal_identity_changed_before_press"] }
        let candidate = freshNodes[freshMatches[0]].element
        guard gaFreshLocalCandidate(candidate, objective: objective, status: status, expectedTime: expectedTime), app.isActive,
              let lastWindow = gaAttribute(root, kAXFocusedWindowAttribute), CFEqual(lastWindow, window) else {
            return ["clicked": false, "error": "goal_identity_changed_before_press"]
        }
        let method = request["method"] as? String ?? "ax_press"
        if method == "verified_click" {
            // AX supplies global top-left coordinates, the same space used by
            // CGEvent. Require a visible small control and an exact live hit test.
            let screens = NSScreen.screens.map { CGDisplayBounds(CGDirectDisplayID(($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value ?? 0)) }
            let settlingDeadline = Date().addingTimeInterval(2)
            var ready: (AXUIElement, CGRect)?
            var diagnostic: [String: Any] = [:]
            var cursorParked = false
            repeat {
                let (settledNodes, complete) = gaWalk(window)
                guard complete, let nativePage = gaPageIndex(settledNodes, name: name) else {
                    return ["clicked": false, "error": "goal_identity_changed_during_settling", "method": method]
                }
                let controls = settledNodes.indices.filter { gaDescendant(settledNodes, index: $0, ancestor: nativePage) && gaCandidate(settledNodes, $0, objective: objective, status: status, expectedTime: expectedTime) }
                guard controls.count == 1 else { return ["clicked": false, "error": "goal_identity_changed_during_settling", "method": method] }
                let current = settledNodes[controls[0]].element
                guard let bounds = gaControlBounds(current), gaClickBoundsValid(bounds, screens: screens) else {
                    return ["clicked": false, "error": "goal_control_bounds_not_verified", "method": method]
                }
                let center = CGPoint(x: bounds.midX, y: bounds.midY)
                let hitReport = gaHitReport(center, candidate: current)
                let active = app.isActive
                let focused = gaAttribute(root, kAXFocusedWindowAttribute)
                let focusedMatches = focused != nil && CFEqual(focused!, window)
                let enabled = (gaAttribute(current, kAXEnabledAttribute) as? NSNumber)?.boolValue == true
                let localMatches = gaFreshLocalCandidate(current, objective: objective, status: status, expectedTime: expectedTime)
                diagnostic = ["bounds": [bounds.minX, bounds.minY, bounds.width, bounds.height], "center": [center.x, center.y], "hit_test": hitReport, "app_active": active, "focused_window_matches": focusedMatches, "control_enabled": enabled, "local_goal_matches": localMatches, "cursor_parked": cursorParked]
                if hitReport["belongs_to_control"] as? Bool == true && active && focusedMatches && enabled && localMatches {
                    ready = (current, bounds); break
                }
                if !cursorParked && hitReport["belongs_to_control"] as? Bool == false && active && focusedMatches && enabled && localMatches,
                   let windowBounds = gaControlBounds(window), let pageBounds = gaControlBounds(settledNodes[nativePage].element),
                   let park = gaSafeParkingPoint(window: windowBounds, page: pageBounds, screens: screens),
                   let move = CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: park, mouseButton: .left) {
                    // Clear a hover tooltip with one non-click move into the
                    // verified native titlebar; every Goal guard is re-read after.
                    move.post(tap: .cghidEventTap)
                    cursorParked = true
                    RunLoop.current.run(until: Date().addingTimeInterval(0.25))
                    continue
                }
                if Date() < settlingDeadline { RunLoop.current.run(until: Date().addingTimeInterval(0.12)) }
            } while Date() < settlingDeadline
            guard let (candidate, bounds) = ready else {
                diagnostic["clicked"] = false; diagnostic["error"] = "goal_control_hit_not_verified"; diagnostic["method"] = method
                return diagnostic
            }
            let center = CGPoint(x: bounds.midX, y: bounds.midY)
            guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: center, mouseButton: .left),
                  let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: center, mouseButton: .left) else {
                return ["clicked": false, "error": "goal_mouse_event_unavailable", "method": method]
            }
            // Single event pair only; acknowledgement is submission, never proof
            // that the Goal entered active state. The owner must confirm execution.
            guard app.isActive, let finalFocused = gaAttribute(root, kAXFocusedWindowAttribute), CFEqual(finalFocused, window),
                  gaControlBounds(candidate) == bounds, gaHitBelongsToControl(center, candidate: candidate) else {
                return ["clicked": false, "error": "goal_control_changed_before_mouse_event", "method": method]
            }
            down.post(tap: .cghidEventTap)
            up.post(tap: .cghidEventTap)
            return ["clicked": true, "source": "accessibility", "acknowledgement": "verified_goal_mouse_event", "method": method, "hit_test_passed": true, "cursor_parked": cursorParked]
        }
        guard method == "ax_press" else { return ["clicked": false, "error": "unsupported_goal_action_method"] }
        var actions: CFArray?
        let actionResult = AXUIElementCopyActionNames(candidate, &actions)
        let actionNames = actions as? [String] ?? []
        // Chromium can omit AXPress from action enumeration for an enabled
        // native button. Identity verification above authorizes precisely one
        // AXPress attempt; no alternate action or retry follows an uncertain result.
        let error = AXUIElementPerformAction(candidate, kAXPressAction as CFString)
        var result: [String: Any] = ["source": "accessibility", "acknowledgement": "AXPress", "action_query_code": actionResult.rawValue, "available_actions": actionNames, "action_result_code": error.rawValue]
        if error == .success { result["clicked"] = true; result["error"] = "" }
        else if error == .actionUnsupported { result["clicked"] = false; result["error"] = "accessibility_press_unsupported" }
        else { result["click_outcome"] = "unknown"; result["error"] = "accessibility_press_outcome_unknown" }
        return result

    }
    if action == "inspect" {
        let (nodes, complete) = gaWalk(root)
        let walkDiagnostic = gaLastWalkDiagnostic
        let texts = nodes.flatMap { $0.text }
        let objective = request["objective"] as? String ?? ""
        let name = request["name"] as? String ?? ""
        var report: [String: Any] = ["walk_diagnostic": walkDiagnostic, "trusted": true, "source": "accessibility", "node_count": nodes.count, "tree_complete": complete,
                "goal_resume_label_count": nodes.filter { $0.text.contains(where: { gaResumeLabels.contains($0) }) }.count,
                "target_name_match_count": name.isEmpty ? 0 : nodes.filter { $0.text.contains(gaNormalize(name)) }.count,
                "objective_exact_match": !objective.isEmpty && gaMatchesObjective(texts, objective),
                "objective_truncated_prefix_match": objective.count >= 16 && texts.contains(where: { $0.hasSuffix("…") && gaNormalize(objective).hasPrefix(String($0.dropLast())) }),
                "goal_status_labels": Array(Set(texts.filter { ["Goal usage limited", "目标使用受限", "Paused goal", "已暂停的目标"].contains($0) })).sorted()]
        report["objective_substring_match_count"] = objective.isEmpty ? 0 : texts.filter { $0.contains(gaNormalize(objective)) }.count
        report["target_name_roles"] = nodes.filter { !name.isEmpty && $0.text.contains(gaNormalize(name)) }.map { node -> [String: Any] in
            return ["role": node.role, "selected": (gaAttribute(node.element, kAXSelectedAttribute) as? NSNumber)?.boolValue ?? false, "value_one": (gaAttribute(node.element, kAXValueAttribute) as? NSNumber)?.intValue == 1, "document": gaAttribute(node.element, kAXDocumentAttribute) as? String ?? "", "url": (gaAttribute(node.element, "AXURL") as? URL)?.absoluteString ?? ""]
        }
        if request["debug_local_goal"] as? Bool == true && !name.isEmpty && texts.contains(gaNormalize(name)) {
            var groups: [[String: Any]] = []
            for i in nodes.indices where nodes[i].text.contains(where: { gaResumeLabels.contains($0) }) {
                var actionList: CFArray?
                let actionCode = AXUIElementCopyActionNames(nodes[i].element, &actionList)
                groups.append(["resume_control_diagnostics": ["role": nodes[i].role, "subrole": gaAttribute(nodes[i].element, kAXSubroleAttribute) as? String ?? "", "enabled": nodes[i].enabled, "action_query_code": actionCode.rawValue, "available_actions": actionList as? [String] ?? []]])
                if let bounds = gaControlBounds(nodes[i].element) {
                    groups.append(["resume_hit_diagnostics": ["bounds": [bounds.minX, bounds.minY, bounds.width, bounds.height], "center": [bounds.midX, bounds.midY], "hit_test": gaHitReport(CGPoint(x: bounds.midX, y: bounds.midY), candidate: nodes[i].element)]])
                }
                var parent = nodes[i].parent
                for depth in 1...2 {
                    guard let p = parent else { break }
                    var local: [[String: Any]] = []
                    for (j, node) in nodes.enumerated() {
                        var cursor: Int? = j
                        for _ in 0..<5 {
                            guard let c = cursor else { break }
                            if c == p { local.append(["role": node.role, "text": node.text.map { String($0.prefix(500)) }]); break }
                            cursor = nodes[c].parent
                        }
                    }
                    if local.count <= 80 { groups.append(["ancestor_depth": depth, "nodes": local]) }
                    parent = nodes[p].parent
                }
            }
            report["local_goal_groups"] = groups
        }
        return report


    }
    return ["clicked": false, "error": "unsupported_accessibility_action"]
}
