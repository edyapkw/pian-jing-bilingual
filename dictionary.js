/* Dictionary popup: hover (desktop) or highlight (phone/tablet) a Chinese word.
   Fetches i18n/dict.json only on first lookup, parses off the main thread in a Web Worker,
   and keeps entries in memory. Supports clickable in-book passage hyperlinks. */
(function () {
  "use strict";

  if (window.__BOOK_DICT__) return;
  window.__BOOK_DICT__ = true;

  var STORAGE_KEY_DICT_ENABLED = "dict.on";
  var DEFAULT_MAX_WORD_LENGTH = 8;
  var MAX_SEGMENTED_WORDS = 4;
  var MAX_SELECTION_CHAR_LENGTH = 24;

  var CHINESE_CHAR_PATTERN = /[\u3400-\u9FFF\uF900-\uFAFF]/;

  var popupElement = null;
  var dictionaryEntries = null;
  var maxWordLength = DEFAULT_MAX_WORD_LENGTH;
  var hoverDebounceTimerId = null;
  var selectionDebounceTimerId = null;
  var isPopupPinned = false;
  var ignoreOutsideClickUntilTimestamp = 0;
  var dictionaryLoadPromise = null;
  var activeLookupGenerationId = 0;
  var JUMP_HIGHLIGHT_DURATION_MS = 2600;
  var fallbackJumpHighlightTimerId = null;

  /* ---- DOM & Viewport Helpers ---- */

  function getElementById(elementId) {
    return document.getElementById(elementId);
  }

  function escapeHtml(rawString) {
    return String(rawString)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function isMobileViewport() {
    return Boolean(window.matchMedia && window.matchMedia("(max-width: 640px)").matches);
  }

  function isTouchPointerDevice() {
    return Boolean(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
  }

  function getViewportBounds() {
    var visualViewport = window.visualViewport;
    if (visualViewport) {
      return {
        left: visualViewport.offsetLeft,
        top: visualViewport.offsetTop,
        width: visualViewport.width,
        height: visualViewport.height
      };
    }
    return {
      left: 0,
      top: 0,
      width: window.innerWidth,
      height: window.innerHeight
    };
  }

  /* ---- Dictionary Enable / Disable State ---- */

  function isDictionaryEnabled() {
    return localStorage.getItem(STORAGE_KEY_DICT_ENABLED) !== "0";
  }

  function setDictionaryEnabled(enabled) {
    localStorage.setItem(STORAGE_KEY_DICT_ENABLED, enabled ? "1" : "0");
    document.body.classList.toggle("dict-on", enabled);
    var toggleButton = getElementById("dict-toggle");
    if (toggleButton) {
      toggleButton.classList.toggle("active", enabled);
      toggleButton.setAttribute("aria-pressed", enabled ? "true" : "false");
    }
    if (!enabled) hidePopup();
  }

  /* ---- Dictionary Fetching & Off-Thread Parsing ---- */

  function getDictionaryJsonUrl() {
    return new URL("i18n/dict.json", document.baseURI).href;
  }

  function fetchDictionaryOnMainThread(url) {
    try {
      var rawGlobalPayload = window.__BOOK_DICT_JSON__;
      if (rawGlobalPayload) {
        var parsed = typeof rawGlobalPayload === "string" ? JSON.parse(rawGlobalPayload) : rawGlobalPayload;
        return Promise.resolve(parsed);
      }
    } catch (err) {}

    return fetch(url).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function parseDictionaryInWebWorker(url) {
    try {
      var rawGlobalPayload = window.__BOOK_DICT_JSON__;
      if (rawGlobalPayload) {
        var parsed = typeof rawGlobalPayload === "string" ? JSON.parse(rawGlobalPayload) : rawGlobalPayload;
        return Promise.resolve(parsed);
      }
    } catch (err) {}

    return new Promise(function (resolve, reject) {
      var workerSource = [
        "self.onmessage=function(e){",
        "fetch(e.data).then(function(r){",
        "if(!r.ok)throw new Error('HTTP '+r.status);",
        "return r.json();",
        "}).then(function(d){self.postMessage({ok:1,d:d});})",
        ".catch(function(err){self.postMessage({ok:0,e:String(err)});});",
        "};"
      ].join("");

      var worker;
      try {
        var scriptBlob = new Blob([workerSource], { type: "application/javascript" });
        worker = new Worker(URL.createObjectURL(scriptBlob));
      } catch (err) {
        fetchDictionaryOnMainThread(url).then(resolve, reject);
        return;
      }

      var isSettled = false;
      function settleWorkerPromise(callback, value) {
        if (isSettled) return;
        isSettled = true;
        try {
          worker.terminate();
        } catch (err) {}
        callback(value);
      }

      worker.onmessage = function (event) {
        var message = event.data || {};
        if (!message.ok) {
          settleWorkerPromise(reject, new Error(message.e || "dictionary parse failed"));
        } else {
          settleWorkerPromise(resolve, message.d);
        }
      };

      worker.onerror = function () {
        settleWorkerPromise(reject, new Error("dictionary worker failed"));
      };

      worker.postMessage(url);
    });
  }

  function ensureDictionaryLoaded() {
    if (dictionaryEntries) return Promise.resolve(dictionaryEntries);
    if (dictionaryLoadPromise) return dictionaryLoadPromise;

    var url = getDictionaryJsonUrl();
    dictionaryLoadPromise = parseDictionaryInWebWorker(url)
      .catch(function () {
        return fetchDictionaryOnMainThread(url);
      })
      .then(function (payload) {
        if (!payload || !payload.entries) {
          throw new Error("dictionary not available");
        }
        dictionaryEntries = payload.entries;
        if (payload.maxLen) maxWordLength = payload.maxLen;
        return dictionaryEntries;
      })
      .catch(function (err) {
        dictionaryLoadPromise = null;
        console.warn("[dict] could not load i18n/dict.json", err);
        throw err;
      });

    return dictionaryLoadPromise;
  }

  /* ---- Word Lookup & Segmentation Logic ---- */

  function isInsideChineseReaderColumn(domNode) {
    var element = domNode && (domNode.nodeType === 3 ? domNode.parentElement : domNode);
    if (!element || !element.closest) return false;
    if (
      element.closest(
        "#dict-popup, #audio-bar, .pair-play-btn, .toolbar, .toc, .hero, .site-footer, .col.en, #back-top"
      )
    ) {
      return false;
    }
    var chineseColumn = element.closest(".col.zh");
    return Boolean(chineseColumn && chineseColumn.closest("#reader"));
  }

  function normalizeExampleItem(rawExample) {
    if (!rawExample) return null;
    if (Array.isArray(rawExample)) {
      return {
        sentence: String(rawExample[0] || ""),
        targetId: String(rawExample[1] || ""),
        locLabel: String(rawExample[2] || "")
      };
    }
    if (typeof rawExample === "object") {
      return {
        sentence: String(rawExample.s || rawExample.sentence || ""),
        targetId: String(rawExample.id || rawExample.targetId || ""),
        locLabel: String(rawExample.loc || rawExample.locLabel || "")
      };
    }
    return {
      sentence: String(rawExample),
      targetId: "",
      locLabel: ""
    };
  }

  function normalizeDictionaryEntry(rawEntry) {
    if (!rawEntry) return null;
    var rawExamples = Array.isArray(rawEntry) ? rawEntry[2] || [] : rawEntry.e || [];
    var examples = [];
    for (var i = 0; i < rawExamples.length; i++) {
      var normalizedEx = normalizeExampleItem(rawExamples[i]);
      if (normalizedEx && normalizedEx.sentence) {
        examples.push(normalizedEx);
      }
    }
    return {
      pinyin: (Array.isArray(rawEntry) ? rawEntry[0] : rawEntry.p) || "",
      definitions: (Array.isArray(rawEntry) ? rawEntry[1] : rawEntry.d) || [],
      examples: examples
    };
  }

  function lookupWordEntry(word) {
    if (!dictionaryEntries || !word) return null;
    return normalizeDictionaryEntry(dictionaryEntries[word]);
  }

  function findLongestMatchingWordAt(text, charIndex) {
    if (
      !dictionaryEntries ||
      charIndex < 0 ||
      charIndex >= text.length ||
      !CHINESE_CHAR_PATTERN.test(text.charAt(charIndex))
    ) {
      return "";
    }
    var longestMatch = "";
    var minStartIndex = Math.max(0, charIndex - maxWordLength + 1);
    for (var start = minStartIndex; start <= charIndex; start++) {
      var maxEndIndex = Math.min(text.length, start + maxWordLength);
      for (var end = charIndex + 1; end <= maxEndIndex; end++) {
        var candidateWord = text.slice(start, end);
        if (candidateWord.length >= longestMatch.length && dictionaryEntries[candidateWord]) {
          longestMatch = candidateWord;
        }
      }
    }
    return longestMatch;
  }

  function segmentChinesePhraseIntoWords(text) {
    var segmentedWords = [];
    var index = 0;
    var length = text.length;

    while (index < length) {
      if (!CHINESE_CHAR_PATTERN.test(text.charAt(index))) {
        index++;
        continue;
      }
      var matchedLength = 1;
      var maxCandidateLength = Math.min(maxWordLength, length - index);
      for (var span = maxCandidateLength; span >= 1; span--) {
        var candidate = text.slice(index, index + span);
        if (dictionaryEntries[candidate]) {
          matchedLength = span;
          break;
        }
      }
      segmentedWords.push(text.slice(index, index + matchedLength));
      index += matchedLength;
      if (segmentedWords.length >= MAX_SEGMENTED_WORDS) break;
    }
    return segmentedWords;
  }

  function resolveLookupWordsForSelection(rawText) {
    var cleanText = String(rawText || "").replace(/\s+/g, "");
    if (!cleanText || !CHINESE_CHAR_PATTERN.test(cleanText)) return [];
    if (dictionaryEntries[cleanText]) return [cleanText];
    if (cleanText.length === 1) return dictionaryEntries[cleanText] ? [cleanText] : [];

    var fullSpanMatch = findLongestMatchingWordAt(cleanText, 0);
    if (fullSpanMatch && fullSpanMatch.length === cleanText.length) {
      return [fullSpanMatch];
    }
    return segmentChinesePhraseIntoWords(cleanText);
  }

  /* ---- Example Hyperlink Resolution & Smooth Jump ---- */

  function findPairElementForExample(targetId, sentenceSnippet, word) {
    if (targetId) {
      var directElement = getElementById(targetId);
      if (directElement) return directElement;
    }
    var readerElement = getElementById("reader");
    if (!readerElement) return null;

    var pairs = readerElement.querySelectorAll(".pair");
    var normalizedSnippet = String(sentenceSnippet || "").replace(/\s+/g, "");
    var probeSubstring = normalizedSnippet.slice(0, 18);

    for (var i = 0; i < pairs.length; i++) {
      var zhCol = pairs[i].querySelector(".col.zh");
      if (!zhCol) continue;
      var zhText = (zhCol.textContent || "").replace(/\s+/g, "");
      if (normalizedSnippet && zhText.indexOf(normalizedSnippet) !== -1) {
        return pairs[i];
      }
      if (probeSubstring && probeSubstring.length >= 6 && zhText.indexOf(probeSubstring) !== -1) {
        return pairs[i];
      }
    }

    if (word) {
      for (var j = 0; j < pairs.length; j++) {
        var zhColFallback = pairs[j].querySelector(".col.zh");
        if (zhColFallback && (zhColFallback.textContent || "").indexOf(word) !== -1) {
          return pairs[j];
        }
      }
    }
    return null;
  }

  function resolvePairLocationLabel(pairElement) {
    if (typeof window.__resolveReaderPairLocation === "function") {
      return window.__resolveReaderPairLocation(pairElement);
    }
    return (pairElement && pairElement.getAttribute("data-loc")) || "跳转原段";
  }

  function pulseJumpHighlight(targetElement) {
    if (typeof window.__pulseReaderJumpHighlight === "function") {
      window.__pulseReaderJumpHighlight(targetElement);
      return;
    }
    if (!targetElement) return;
    if (fallbackJumpHighlightTimerId) clearTimeout(fallbackJumpHighlightTimerId);
    targetElement.classList.add("jump-highlight");
    fallbackJumpHighlightTimerId = setTimeout(function () {
      targetElement.classList.remove("jump-highlight");
    }, JUMP_HIGHLIGHT_DURATION_MS);
  }

  function jumpToBookExample(targetId, sentenceSnippet, word) {
    var targetElement = findPairElementForExample(targetId, sentenceSnippet, word);
    if (!targetElement) return;

    isPopupPinned = false;
    hidePopup();

    if (targetElement.id && history.replaceState) {
      try {
        history.replaceState(null, "", "#" + targetElement.id);
      } catch (err) {}
    }

    try {
      targetElement.scrollIntoView({ behavior: "smooth", block: "center" });
    } catch (err) {
      targetElement.scrollIntoView();
    }

    pulseJumpHighlight(targetElement);
  }

  /* ---- Popup Rendering & Positioning ---- */

  function highlightWordInSnippet(snippet, word) {
    var matchIndex = snippet.indexOf(word);
    if (matchIndex < 0) return escapeHtml(snippet);
    return (
      escapeHtml(snippet.slice(0, matchIndex)) +
      "<mark>" +
      escapeHtml(word) +
      "</mark>" +
      escapeHtml(snippet.slice(matchIndex + word.length))
    );
  }

  function renderExampleLinkItemHtml(exampleObj, word) {
    var targetId = exampleObj.targetId || "";
    var locLabel = exampleObj.locLabel || "";
    if (!targetId || !locLabel) {
      var matchedPair = findPairElementForExample(targetId, exampleObj.sentence, word);
      if (matchedPair) {
        if (!targetId) targetId = matchedPair.id || "";
        if (!locLabel) locLabel = resolvePairLocationLabel(matchedPair);
      }
    }
    var hrefAttr = targetId ? "#" + escapeHtml(targetId) : "#reader";
    var badgeText = locLabel || "跳转原段";

    return (
      "<li>" +
        '<a class="dict-example-link" href="' +
        hrefAttr +
        '" data-target-id="' +
        escapeHtml(targetId) +
        '" data-word="' +
        escapeHtml(word) +
        '" data-snippet="' +
        escapeHtml(exampleObj.sentence) +
        '" title="Jump to ' +
        escapeHtml(badgeText) +
        '">' +
          '<span class="dict-ex-text">' +
          highlightWordInSnippet(exampleObj.sentence, word) +
          "</span>" +
          '<span class="dict-ex-meta">' +
            '<span class="dict-ex-loc">' +
            escapeHtml(badgeText) +
            "</span>" +
            '<span class="dict-ex-jump">跳转 ↗</span>' +
          "</span>" +
        "</a>" +
      "</li>"
    );
  }

  function renderSingleWordEntryHtml(word) {
    var entry = lookupWordEntry(word);
    var html = '<div class="dict-entry">';
    html += '<div class="dict-word">' + escapeHtml(word) + "</div>";

    if (entry && entry.pinyin) {
      html += '<div class="dict-pinyin">' + escapeHtml(entry.pinyin) + "</div>";
    }
    if (entry && entry.definitions && entry.definitions.length) {
      html += '<ol class="dict-defs">';
      for (var i = 0; i < entry.definitions.length; i++) {
        html += "<li>" + escapeHtml(entry.definitions[i]) + "</li>";
      }
      html += "</ol>";
    } else {
      html += '<p class="dict-empty">No dictionary entry for this word.</p>';
    }

    if (entry && entry.examples && entry.examples.length) {
      html += '<div class="dict-ex-label">In this book · 点击跳转章节</div><ul class="dict-examples">';
      for (var j = 0; j < entry.examples.length; j++) {
        html += renderExampleLinkItemHtml(entry.examples[j], word);
      }
      html += "</ul>";
    }
    html += "</div>";
    return html;
  }

  function renderDictionaryEntriesHtml(words) {
    var html = "";
    for (var i = 0; i < words.length; i++) {
      html += renderSingleWordEntryHtml(words[i]);
    }
    return html;
  }

  function ensurePopupElement() {
    if (popupElement) return popupElement;
    popupElement = document.createElement("div");
    popupElement.id = "dict-popup";
    popupElement.hidden = true;
    popupElement.setAttribute("role", "dialog");
    popupElement.setAttribute("aria-label", "词典");
    popupElement.innerHTML =
      '<button type="button" class="dict-close" aria-label="关闭">×</button>' +
      '<div class="dict-body"></div>' +
      '<div class="dict-credit">Meanings: CC-CEDICT</div>';
    document.body.appendChild(popupElement);

    popupElement.querySelector(".dict-close").addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      isPopupPinned = false;
      hidePopup();
    });

    popupElement.addEventListener("click", function (event) {
      var linkEl =
        event.target && event.target.closest
          ? event.target.closest(".dict-example-link")
          : null;
      if (!linkEl) return;
      event.preventDefault();
      event.stopPropagation();
      jumpToBookExample(
        linkEl.getAttribute("data-target-id") || "",
        linkEl.getAttribute("data-snippet") || "",
        linkEl.getAttribute("data-word") || ""
      );
    });

    popupElement.addEventListener("mouseenter", function () {
      if (hoverDebounceTimerId) clearTimeout(hoverDebounceTimerId);
    });
    popupElement.addEventListener("mouseleave", function () {
      if (!isPopupPinned && !isTouchPointerDevice()) scheduleHidePopup();
    });
    return popupElement;
  }

  function calculatePopupCoordinates(targetRect, popupWidth, popupHeight) {
    var edgePadding = 10;
    var verticalGap = isTouchPointerDevice() ? 48 : 10;
    var viewport = getViewportBounds();

    var left = targetRect.left + targetRect.width / 2 - popupWidth / 2;
    left = Math.max(
      viewport.left + edgePadding,
      Math.min(left, viewport.left + viewport.width - popupWidth - edgePadding)
    );

    var topBelow = targetRect.bottom + verticalGap;
    var fitsBelow = topBelow + popupHeight <= viewport.top + viewport.height - edgePadding;
    var topAbove = targetRect.top - popupHeight - verticalGap;
    var top = !fitsBelow && topAbove >= viewport.top + edgePadding ? topAbove : topBelow;
    top = Math.max(
      viewport.top + edgePadding,
      Math.min(top, viewport.top + viewport.height - popupHeight - edgePadding)
    );

    return { left: Math.round(left), top: Math.round(top) };
  }

  function positionPopupNearRect(targetRect) {
    var box = ensurePopupElement();
    box.hidden = false;
    if (isMobileViewport()) {
      box.classList.add("dict-sheet");
      box.style.left = "";
      box.style.top = "";
      return;
    }
    box.classList.remove("dict-sheet");
    var coords = calculatePopupCoordinates(targetRect, box.offsetWidth, box.offsetHeight);
    box.style.left = coords.left + "px";
    box.style.top = coords.top + "px";
  }

  function showPopupForWords(words, targetRect, shouldPin) {
    if (!words || !words.length) return;
    isPopupPinned = Boolean(shouldPin);
    ignoreOutsideClickUntilTimestamp = Date.now() + 500;
    var box = ensurePopupElement();
    box.querySelector(".dict-body").innerHTML = renderDictionaryEntriesHtml(words);
    box.hidden = false;
    positionPopupNearRect(targetRect);
  }

  function showPopupStatusMessage(message, targetRect, shouldPin) {
    isPopupPinned = Boolean(shouldPin);
    var box = ensurePopupElement();
    box.querySelector(".dict-body").innerHTML =
      '<p class="dict-empty">' + escapeHtml(message) + "</p>";
    box.hidden = false;
    positionPopupNearRect(targetRect);
  }

  function hidePopup() {
    activeLookupGenerationId += 1;
    if (hoverDebounceTimerId) {
      clearTimeout(hoverDebounceTimerId);
      hoverDebounceTimerId = null;
    }
    if (popupElement) popupElement.hidden = true;
    isPopupPinned = false;
  }

  function scheduleHidePopup() {
    if (hoverDebounceTimerId) clearTimeout(hoverDebounceTimerId);
    hoverDebounceTimerId = setTimeout(function () {
      if (!isPopupPinned) hidePopup();
    }, 180);
  }

  function runWithLoadedDictionary(generationId, targetRect, shouldPin, onReadyCallback) {
    if (!dictionaryEntries) {
      showPopupStatusMessage("Loading dictionary…", targetRect, shouldPin);
    }
    ensureDictionaryLoaded()
      .then(function () {
        if (generationId !== activeLookupGenerationId) return;
        onReadyCallback();
      })
      .catch(function () {
        if (generationId !== activeLookupGenerationId) return;
        showPopupStatusMessage("Dictionary could not be loaded.", targetRect, true);
      });
  }

  /* ---- Range & Selection Inspection ---- */

  function createCaretRangeFromPoint(clientX, clientY) {
    if (document.caretRangeFromPoint) {
      return document.caretRangeFromPoint(clientX, clientY);
    }
    if (document.caretPositionFromPoint) {
      var position = document.caretPositionFromPoint(clientX, clientY);
      if (!position) return null;
      var range = document.createRange();
      range.setStart(position.offsetNode, position.offset);
      range.collapse(true);
      return range;
    }
    return null;
  }

  function createDomRangeForSubstring(textNode, startOffset, word) {
    var range = document.createRange();
    range.setStart(textNode, startOffset);
    range.setEnd(textNode, startOffset + word.length);
    return range;
  }

  function findWordStartIndexAroundOffset(fullText, word, charOffset) {
    var startIndex = fullText.lastIndexOf(word, charOffset);
    if (startIndex < 0 || startIndex + word.length <= charOffset) {
      startIndex = fullText.indexOf(word, Math.max(0, charOffset - word.length + 1));
    }
    return startIndex >= 0 ? startIndex : charOffset;
  }

  function lookupWordAtPoint(clientX, clientY) {
    if (!isDictionaryEnabled() || isPopupPinned) return;
    var caretRange = createCaretRangeFromPoint(clientX, clientY);
    if (!caretRange || !caretRange.startContainer || caretRange.startContainer.nodeType !== 3) {
      scheduleHidePopup();
      return;
    }

    var textNode = caretRange.startContainer;
    if (!isInsideChineseReaderColumn(textNode)) {
      scheduleHidePopup();
      return;
    }

    var text = textNode.nodeValue || "";
    var charIndex = Math.min(caretRange.startOffset, Math.max(0, text.length - 1));
    if (charIndex < text.length && !CHINESE_CHAR_PATTERN.test(text.charAt(charIndex)) && charIndex > 0) {
      charIndex -= 1;
    }
    if (!text || !CHINESE_CHAR_PATTERN.test(text.charAt(charIndex))) {
      scheduleHidePopup();
      return;
    }

    var generationId = ++activeLookupGenerationId;
    var initialCharRange = createDomRangeForSubstring(textNode, charIndex, text.charAt(charIndex) || "字");

    runWithLoadedDictionary(
      generationId,
      initialCharRange.getBoundingClientRect(),
      false,
      function () {
        var matchedWord = findLongestMatchingWordAt(text, charIndex);
        if (!matchedWord) {
          scheduleHidePopup();
          return;
        }
        var wordStart = findWordStartIndexAroundOffset(text, matchedWord, charIndex);
        var wordRange = createDomRangeForSubstring(textNode, wordStart, matchedWord);
        showPopupForWords([matchedWord], wordRange.getBoundingClientRect(), false);
      }
    );
  }

  function handleReaderMouseMove(event) {
    if (!isDictionaryEnabled() || isPopupPinned || isTouchPointerDevice()) return;
    if (event.buttons) return;
    if (hoverDebounceTimerId) clearTimeout(hoverDebounceTimerId);
    var clientX = event.clientX;
    var clientY = event.clientY;
    hoverDebounceTimerId = setTimeout(function () {
      lookupWordAtPoint(clientX, clientY);
    }, 220);
  }

  function getSelectedChineseTextAndRect() {
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
    var range = selection.getRangeAt(0);
    if (!isInsideChineseReaderColumn(range.commonAncestorContainer)) return null;
    var selectedText = String(selection).replace(/\s+/g, "");
    if (!CHINESE_CHAR_PATTERN.test(selectedText) || selectedText.length > MAX_SELECTION_CHAR_LENGTH) {
      return null;
    }
    return { text: selectedText, rect: range.getBoundingClientRect() };
  }

  function handleTextSelectionChange() {
    if (!isDictionaryEnabled()) return;
    if (selectionDebounceTimerId) clearTimeout(selectionDebounceTimerId);
    selectionDebounceTimerId = setTimeout(function () {
      var selectionInfo = getSelectedChineseTextAndRect();
      if (!selectionInfo) return;
      var generationId = ++activeLookupGenerationId;
      runWithLoadedDictionary(generationId, selectionInfo.rect, true, function () {
        var words = resolveLookupWordsForSelection(selectionInfo.text);
        if (!words.length) {
          showPopupStatusMessage("No dictionary entry for this word.", selectionInfo.rect, true);
          return;
        }
        showPopupForWords(words, selectionInfo.rect, true);
      });
    }, isTouchPointerDevice() ? 280 : 40);
  }

  function handleWindowScroll() {
    if (popupElement && !popupElement.hidden && !isMobileViewport()) {
      hidePopup();
    }
  }

  /* ---- Toolbar & Floating Controls Setup ---- */

  function ensureDictionaryToggleButton() {
    if (getElementById("dict-toggle")) return;
    var toolbarInner = document.querySelector(".toolbar-inner");
    if (!toolbarInner) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "btn";
    button.id = "dict-toggle";
    button.title = "Dictionary";
    button.setAttribute("aria-label", "词典");
    button.textContent = "典";
    toolbarInner.appendChild(button);
  }

  function ensureBackToTopButton() {
    if (getElementById("back-top")) return;
    var button = document.createElement("button");
    button.id = "back-top";
    button.type = "button";
    button.setAttribute("aria-label", "回到顶部");
    button.textContent = "↑";
    button.addEventListener("click", function () {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    document.body.appendChild(button);
    window.addEventListener(
      "scroll",
      function () {
        button.classList.toggle("visible", window.scrollY > 700);
      },
      { passive: true }
    );
  }

  function bindDictionaryEvents() {
    var readerElement = getElementById("reader");
    if (!readerElement) return;

    ensureDictionaryToggleButton();
    ensureBackToTopButton();

    readerElement.addEventListener("mousemove", handleReaderMouseMove);
    readerElement.addEventListener("mouseleave", function (event) {
      if (isMobileViewport() || isTouchPointerDevice()) return;
      if (popupElement && popupElement.contains(event.relatedTarget)) return;
      if (!isPopupPinned) scheduleHidePopup();
    });

    document.addEventListener("mouseup", handleTextSelectionChange);
    document.addEventListener("touchend", handleTextSelectionChange);
    document.addEventListener("selectionchange", function () {
      if (isTouchPointerDevice()) handleTextSelectionChange();
    });

    window.addEventListener("scroll", handleWindowScroll, { passive: true });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") hidePopup();
    });

    document.addEventListener("click", function (event) {
      if (!popupElement || popupElement.hidden) return;
      if (Date.now() < ignoreOutsideClickUntilTimestamp) return;
      if (
        popupElement.contains(event.target) ||
        (event.target && event.target.id === "dict-toggle")
      ) {
        return;
      }
      var selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      hidePopup();
    });

    var toggleButton = getElementById("dict-toggle");
    if (toggleButton) {
      toggleButton.addEventListener("click", function () {
        setDictionaryEnabled(!isDictionaryEnabled());
      });
    }
  }

  function initDictionary() {
    bindDictionaryEvents();
    setDictionaryEnabled(isDictionaryEnabled());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initDictionary);
  } else {
    initDictionary();
  }
})();
