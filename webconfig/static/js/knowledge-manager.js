const KnowledgeManager = (() => {
    let selectedFolderId = "";
    let isCreateFolderOpen = false;
    let isUploadOpen = false;
    let elements = {};

    const refreshElements = () => {
        elements = {
            foldersList: document.getElementById("knowledge-folders-list"),
            documentsList: document.getElementById("knowledge-documents-list"),
            summary: document.getElementById("knowledge-summary"),
            selectedFolderLabel: document.getElementById("knowledge-selected-folder-label"),
            selectedFolderStats: document.getElementById("knowledge-selected-folder-stats"),
            showCreateFolderBtn: document.getElementById("knowledge-show-create-folder-btn"),
            createFolderPanel: document.getElementById("knowledge-create-folder-panel"),
            cancelCreateFolderBtn: document.getElementById("knowledge-cancel-create-folder-btn"),
            showUploadBtn: document.getElementById("knowledge-show-upload-btn"),
            showUploadIcon: document.getElementById("knowledge-show-upload-icon"),
            uploadPanel: document.getElementById("knowledge-upload-panel"),
            newFolderInput: document.getElementById("knowledge-new-folder-input"),
            createFolderBtn: document.getElementById("knowledge-create-folder-btn"),
            fileInput: document.getElementById("knowledge-file-input"),
            triggerTopics: document.getElementById("knowledge-trigger-topics"),
            uploadBtn: document.getElementById("knowledge-upload-btn"),
            reindexBtn: document.getElementById("knowledge-reindex-btn"),
            saveTopicsBtn: document.getElementById("knowledge-save-topics-btn"),
        };
        return elements;
    };

    const escapeHtml = (value) => String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");

    const middleEllipsis = (value, maxLength = 44) => {
        const text = String(value || "");
        if (text.length <= maxLength) return text;

        const lastDot = text.lastIndexOf(".");
        const extension = lastDot > 0 ? text.slice(lastDot) : "";
        const basename = lastDot > 0 ? text.slice(0, lastDot) : text;
        const extensionBudget = Math.min(extension.length, 10);
        const totalVisible = Math.max(maxLength - extensionBudget - 1, 10);
        const front = Math.ceil(totalVisible * 0.58);
        const back = Math.max(totalVisible - front, 4);

        return `${basename.slice(0, front)}...${basename.slice(-back)}${extension}`;
    };

    const renderSummary = (summary = {}) => {
        refreshElements();
        if (!elements.summary) return;
        const folders = summary.folder_count || 0;
        const documents = summary.document_count || 0;
        const chunks = summary.chunk_count || 0;
        elements.summary.textContent = `${folders} folder${folders === 1 ? "" : "s"} • ${documents} file${documents === 1 ? "" : "s"} • ${chunks} chunk${chunks === 1 ? "" : "s"}`;
    };

    const setCreateFolderOpen = (open) => {
        refreshElements();
        isCreateFolderOpen = open;
        if (elements.createFolderPanel) {
            elements.createFolderPanel.classList.toggle("hidden", !open);
        }
        if (!open && elements.newFolderInput) {
            elements.newFolderInput.value = "";
        }
        if (open) {
            elements.newFolderInput?.focus();
        }
    };

    const setUploadOpen = (open) => {
        refreshElements();
        isUploadOpen = open;
        if (elements.uploadPanel) {
            elements.uploadPanel.classList.toggle("hidden", !open);
        }
        if (elements.showUploadIcon) {
            elements.showUploadIcon.textContent = open ? "close" : "add";
        }
        if (elements.showUploadBtn) {
            elements.showUploadBtn.title = open ? "Close upload" : "Add file";
            elements.showUploadBtn.classList.toggle("secondary-action--hover--emerald", !open);
            elements.showUploadBtn.classList.toggle("secondary-action--hover--rose", open);
        }
        if (!open && elements.fileInput) {
            elements.fileInput.value = "";
        }
    };

    const syncSelectedFolder = (folders = []) => {
        refreshElements();
        if (!folders.length) {
            selectedFolderId = "";
            if (elements.triggerTopics) {
                elements.triggerTopics.value = "";
                elements.triggerTopics.disabled = true;
            }
            if (elements.fileInput) {
                elements.fileInput.disabled = true;
            }
            if (elements.uploadBtn) {
                elements.uploadBtn.disabled = true;
                elements.uploadBtn.classList.add("opacity-50", "cursor-not-allowed");
            }
            if (elements.saveTopicsBtn) {
                elements.saveTopicsBtn.disabled = true;
                elements.saveTopicsBtn.classList.add("opacity-50", "cursor-not-allowed");
            }
            if (elements.reindexBtn) {
                elements.reindexBtn.disabled = true;
                elements.reindexBtn.classList.add("opacity-50", "cursor-not-allowed");
            }
            return null;
        }

        const current = folders.find((folder) => folder.id === selectedFolderId) || folders[0];
        selectedFolderId = current.id;

        if (elements.triggerTopics) {
            elements.triggerTopics.disabled = false;
            elements.triggerTopics.value = (current.trigger_topics || []).join(", ");
        }
        if (elements.fileInput) {
            elements.fileInput.disabled = false;
        }
        if (elements.uploadBtn) {
            elements.uploadBtn.disabled = false;
            elements.uploadBtn.classList.remove("opacity-50", "cursor-not-allowed");
        }
        if (elements.saveTopicsBtn) {
            elements.saveTopicsBtn.disabled = false;
            elements.saveTopicsBtn.classList.remove("opacity-50", "cursor-not-allowed");
        }
        if (elements.reindexBtn) {
            elements.reindexBtn.disabled = false;
            elements.reindexBtn.classList.remove("opacity-50", "cursor-not-allowed");
        }

        return current;
    };

    const renderFolders = (folders = []) => {
        refreshElements();
        if (!elements.foldersList) return;
        if (!folders.length) {
            elements.foldersList.innerHTML = '<div class="text-sm text-zinc-400">No folders yet.</div>';
            return;
        }

        elements.foldersList.innerHTML = folders.map((folder) => {
            const isSelected = folder.id === selectedFolderId;
            const triggerText = (folder.trigger_topics || []).length
                ? escapeHtml(folder.trigger_topics.join(", "))
                : "No trigger topics";
            const canDelete = folders.length > 1;

            return `
                <div
                    class="knowledge-folder-card rounded-lg border ${isSelected ? "border-emerald-500 bg-zinc-900/70" : "border-zinc-700 bg-zinc-950/40"} p-3"
                    data-folder-id="${escapeHtml(folder.id)}"
                >
                    <div class="flex items-center justify-between gap-3">
                        <button type="button" class="knowledge-folder-select min-w-0 flex-1 text-left cursor-pointer">
                            <div class="text-sm text-slate-100">${escapeHtml(folder.label)}</div>
                            <div class="text-xs text-zinc-400 mt-1 break-words">${triggerText}</div>
                        </button>
                        <div class="flex items-center gap-3 shrink-0">
                            <div class="text-xs text-zinc-400 text-right">
                                <div>${folder.document_count} file${folder.document_count === 1 ? "" : "s"}</div>
                                <div>${folder.chunk_count} chunks</div>
                            </div>
                            <button
                                type="button"
                                class="knowledge-delete-folder-row-btn secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0 ${canDelete ? "" : "opacity-50 cursor-not-allowed"}"
                                data-folder-id="${escapeHtml(folder.id)}"
                                data-folder-label="${escapeHtml(folder.label)}"
                                ${canDelete ? "" : "disabled"}
                                title="Delete folder"
                            >
                                <span class="material-icons">delete</span>
                            </button>
                        </div>
                    </div>
                </div>
            `;
        }).join("");

        elements.foldersList.querySelectorAll(".knowledge-folder-select").forEach((button) => {
            button.addEventListener("click", () => {
                const card = button.closest(".knowledge-folder-card");
                selectedFolderId = card?.dataset.folderId || "";
                window.MobileSplitView?.showDetail("knowledge-split-view");
                loadKnowledgeState();
            });
        });

        elements.foldersList.querySelectorAll(".knowledge-delete-folder-row-btn").forEach((button) => {
            button.addEventListener("click", async () => {
                if (button.disabled) {
                    return;
                }
                const folderId = button.dataset.folderId || "";
                const folderLabel = button.dataset.folderLabel || "this folder";
                await deleteFolder(folderId, folderLabel);
            });
        });
    };

    const renderDocuments = (folder) => {
        refreshElements();
        if (!elements.documentsList) return;
        const documents = folder?.documents || [];

        if (!documents.length) {
            elements.documentsList.innerHTML = '<div class="text-sm text-zinc-400">No files in this folder yet.</div>';
            return;
        }

        elements.documentsList.innerHTML = documents.map((doc) => {
            const uploaded = new Date(doc.uploaded_at).toLocaleString();
            const displayName = middleEllipsis(doc.filename, 44);
            return `
                <div class="rounded border border-zinc-700 bg-zinc-950/60 p-3 flex flex-col md:flex-row md:items-center md:justify-between gap-3">
                    <div class="min-w-0 flex-1 overflow-hidden">
                        <div class="text-sm text-slate-100 whitespace-nowrap overflow-hidden" title="${escapeHtml(doc.filename)}">${escapeHtml(displayName)}</div>
                        <div class="text-xs text-zinc-400 mt-1">
                            ${escapeHtml(doc.extension)} • ${doc.chunk_count} chunks • ${escapeHtml(uploaded)}
                        </div>
                    </div>
                    <button
                        type="button"
                        class="knowledge-delete-file-btn secondary-action secondary-action--hover--rose h-11 w-11 p-0 shrink-0 self-start md:self-auto"
                        data-document-id="${escapeHtml(doc.id)}"
                        data-filename="${escapeHtml(doc.filename)}"
                        title="Delete file"
                    >
                        <span class="material-icons">delete</span>
                    </button>
                </div>
            `;
        }).join("");

        elements.documentsList.querySelectorAll(".knowledge-delete-file-btn").forEach((button) => {
            button.addEventListener("click", async () => {
                const documentId = button.dataset.documentId || "";
                const filename = button.dataset.filename || "this file";
                if (!window.confirm(`Delete ${filename}?`)) {
                    return;
                }
                try {
                    const response = await fetch(`/knowledge/documents/${documentId}`, {
                        method: "DELETE",
                    });
                    const data = await response.json();
                    if (!response.ok) {
                        throw new Error(data.error || "Failed to delete file");
                    }
                    showNotification("Knowledge file deleted", "success");
                    renderKnowledgeState(data);
                } catch (error) {
                    console.error("Knowledge delete failed:", error);
                    showNotification(error.message || "Failed to delete file", "error");
                }
            });
        });
    };

    const renderSelectedFolder = (folder) => {
        refreshElements();
        if (elements.selectedFolderLabel) {
            elements.selectedFolderLabel.textContent = folder?.label || "Selected Folder";
        }
        if (elements.selectedFolderStats) {
            const documentCount = folder?.document_count || 0;
            const chunkCount = folder?.chunk_count || 0;
            elements.selectedFolderStats.textContent = `${documentCount} file${documentCount === 1 ? "" : "s"} • ${chunkCount} chunks`;
        }
        renderDocuments(folder);
    };

    const renderKnowledgeState = (data = {}) => {
        const folders = data.folders || [];
        renderSummary(data.summary || {});
        const current = syncSelectedFolder(folders);
        renderFolders(folders);
        renderSelectedFolder(current);
    };

    const loadKnowledgeState = async () => {
        refreshElements();
        try {
            const response = await fetch("/knowledge/folders");
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Failed to load knowledge folders");
            }
            renderKnowledgeState(data);
        } catch (error) {
            console.error("Failed to load knowledge folders:", error);
            showNotification(error.message || "Failed to load knowledge folders", "error");
        }
    };

    const createFolder = async () => {
        refreshElements();
        const label = elements.newFolderInput?.value?.trim() || "";
        if (!label) {
            showNotification("Enter a folder name", "warning");
            return;
        }

        try {
            if (elements.createFolderBtn) {
                elements.createFolderBtn.disabled = true;
                elements.createFolderBtn.classList.add("opacity-50", "cursor-not-allowed");
            }
            const response = await fetch("/knowledge/folders", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ label }),
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Failed to create folder");
            }
            selectedFolderId = data.folder?.id || selectedFolderId;
            if (elements.newFolderInput) {
                elements.newFolderInput.value = "";
            }
            setCreateFolderOpen(false);
            showNotification("Knowledge folder created", "success", 3000);
            renderKnowledgeState(data);
        } catch (error) {
            console.error("Knowledge folder creation failed:", error);
            showNotification(error.message || "Failed to create folder", "error", 3500);
        } finally {
            if (elements.createFolderBtn) {
                elements.createFolderBtn.disabled = false;
                elements.createFolderBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        }
    };

    const deleteFolder = async (folderId = selectedFolderId, folderLabel = elements.selectedFolderLabel?.textContent || "this folder") => {
        refreshElements();
        if (!folderId) {
            showNotification("Select a folder first", "warning");
            return;
        }

        if (!window.confirm(`Delete ${folderLabel} and all files inside it?`)) {
            return;
        }

        try {
            const response = await fetch(`/knowledge/folders/${folderId}`, {
                method: "DELETE",
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Failed to delete folder");
            }
            if (selectedFolderId === folderId) {
                selectedFolderId = "";
            }
            showNotification("Knowledge folder deleted", "success", 3000);
            renderKnowledgeState(data);
        } catch (error) {
            console.error("Knowledge folder delete failed:", error);
            showNotification(error.message || "Failed to delete folder", "error", 3500);
        }
    };

    const uploadDocument = async () => {
        refreshElements();
        const file = elements.fileInput?.files?.[0];
        if (!file) {
            showNotification("Choose a file to upload", "warning");
            return;
        }
        if (!selectedFolderId) {
            showNotification("Select a folder first", "warning");
            return;
        }

        const formData = new FormData();
        formData.append("file", file);
        formData.append("folder_id", selectedFolderId);

        try {
            if (elements.uploadBtn) {
                elements.uploadBtn.disabled = true;
                elements.uploadBtn.classList.add("opacity-50", "cursor-not-allowed");
            }

            const response = await fetch("/knowledge/documents", {
                method: "POST",
                body: formData,
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Upload failed");
            }

            if (elements.fileInput) {
                elements.fileInput.value = "";
            }
            setUploadOpen(false);
            showNotification("Knowledge file uploaded and indexed", "success", 3000);
            renderKnowledgeState(data);
        } catch (error) {
            console.error("Knowledge upload failed:", error);
            showNotification(error.message || "Upload failed", "error", 3500);
        } finally {
            if (elements.uploadBtn) {
                elements.uploadBtn.disabled = false;
                elements.uploadBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        }
    };

    const reindex = async () => {
        refreshElements();
        if (!selectedFolderId) {
            showNotification("Select a folder first", "warning");
            return;
        }
        try {
            if (elements.reindexBtn) {
                elements.reindexBtn.disabled = true;
                elements.reindexBtn.classList.add("opacity-50", "cursor-not-allowed");
            }
            const response = await fetch("/knowledge/reindex", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ folder_id: selectedFolderId }),
            });
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || "Reindex failed");
            }
            showNotification(
                `Reindexed ${data.documents_indexed || 0} file(s)`,
                "success",
                3000
            );
            renderKnowledgeState(data);
        } catch (error) {
            console.error("Knowledge reindex failed:", error);
            showNotification(error.message || "Reindex failed", "error");
        } finally {
            if (elements.reindexBtn) {
                elements.reindexBtn.disabled = false;
                elements.reindexBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        }
    };

    const saveFolderSettings = async () => {
        refreshElements();
        if (!selectedFolderId) {
            showNotification("Select a folder first", "warning");
            return;
        }

        const topics = elements.triggerTopics?.value || "";
        try {
            if (elements.saveTopicsBtn) {
                elements.saveTopicsBtn.disabled = true;
                elements.saveTopicsBtn.classList.add("opacity-50", "cursor-not-allowed");
            }

            const saveResponse = await fetch(`/knowledge/folders/${selectedFolderId}`, {
                method: "PATCH",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ trigger_topics: topics }),
            });
            const saveData = await saveResponse.json();
            if (!saveResponse.ok || !saveData.ok) {
                throw new Error(saveData.error || "Failed to save folder");
            }
            showNotification("Folder trigger topics saved", "success", 3000);
            renderKnowledgeState(saveData);
        } catch (error) {
            console.error("Failed to save folder settings:", error);
            showNotification(error.message || "Failed to save folder", "error", 3500);
        } finally {
            if (elements.saveTopicsBtn) {
                elements.saveTopicsBtn.disabled = false;
                elements.saveTopicsBtn.classList.remove("opacity-50", "cursor-not-allowed");
            }
        }
    };

    const bindUI = () => {
        refreshElements();
        const hasKnowledgeUi = !!(
            elements.foldersList &&
            elements.documentsList &&
            elements.showCreateFolderBtn &&
            elements.showUploadBtn
        );
        if (!hasKnowledgeUi) return;

        const root = elements.foldersList?.closest("#main-content");
        if (root?.dataset.knowledgeBound === "true") {
            setCreateFolderOpen(false);
            setUploadOpen(false);
            loadKnowledgeState();
            return;
        }

        if (root) {
            root.dataset.knowledgeBound = "true";
        }

        elements.showCreateFolderBtn?.addEventListener("click", () => setCreateFolderOpen(!isCreateFolderOpen));
        elements.cancelCreateFolderBtn?.addEventListener("click", () => setCreateFolderOpen(false));
        elements.showUploadBtn?.addEventListener("click", () => setUploadOpen(!isUploadOpen));
        elements.createFolderBtn?.addEventListener("click", createFolder);
        elements.uploadBtn?.addEventListener("click", uploadDocument);
        elements.reindexBtn?.addEventListener("click", reindex);
        elements.saveTopicsBtn?.addEventListener("click", saveFolderSettings);

        elements.newFolderInput?.addEventListener("keypress", (event) => {
            if (event.key === "Enter") {
                event.preventDefault();
                createFolder();
            }
        });

        setCreateFolderOpen(false);
        setUploadOpen(false);
        loadKnowledgeState();
    };

    return { bindUI, loadKnowledgeState };
})();

window.KnowledgeManager = KnowledgeManager;
