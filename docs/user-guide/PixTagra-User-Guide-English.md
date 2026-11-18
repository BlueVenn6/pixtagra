# PixTagra User Guide

For Windows desktop version 0.9.1.0. Updated November 12, 2026.

[简体中文使用说明](PixTagra-使用说明-中文.md)

PixTagra generates, checks, and edits image titles, descriptions, and keywords. You can then save image copies with embedded metadata or export files for stock submission workflows. AI output is a draft for you to review. **Ready** means the current automated checks passed; it does not mean a stock agency has approved the content.

## 1. Quick start

1. Open PixTagra. Under **Language / output**, choose **English** or **简体中文**, then choose a generation mode.
2. Select a model you can access and enter its API key. If you use a relay, enter the endpoint supplied by that relay.
3. Click **Test Connection** and confirm that the model and endpoint work.
4. Click **Import Images**. Start with a small set while learning the workflow.
5. Check the images you want to process and click **Generate metadata**. Wait for generation and review to finish.
6. Check each image's title, description, keywords, and quality messages. Correct factual mistakes, repetition, and missing information.
7. Expand **Save / Export**. Use a save action for image copies, or choose a destination agency and click **Export** for agency files.

Complete one small import → generate → review → save or export cycle before processing a large collection.

## 2. Installation and launch

- **Installer:** run the supplied PixTagra setup program, follow the wizard, and launch PixTagra from the Windows Start menu. The default location is `%LOCALAPPDATA%\Programs\PixTagra` for the current user.
- **Portable ZIP:** extract the entire ZIP before running `PixTagra.exe`. Keep `_internal` and all other supplied files together. Do not copy only the EXE or run it from inside the ZIP.
- The application requires 64-bit Windows 10 or later. Neither distribution requires a separate Python installation.
- The default workspace is owned by one application instance at a time. If it is locked, close other PixTagra windows normally, then reopen the application.

## 3. Language and generation modes

### Language

**Language / output** controls both the interface and the output language for new generation requests. English is the initial default; the application does not choose a language from your operating system. Normally saved settings are read on later launches.

| Selection | Newly generated titles, descriptions, and keywords |
| --- | --- |
| English | English |
| 简体中文 | Simplified Chinese |

**Changing the language does not translate existing results.** For example, Chinese records stay Chinese after you choose English. Generate again with English selected if you need English content. Process each language in a separate batch to avoid language conflicts during agency export.

### Generation modes

| Mode | Suitable images | Main focus |
| --- | --- | --- |
| General Photography | People, animals, food, buildings, interiors, travel, and everyday scenes | The subject, action, visible attributes, supporting objects, and setting |
| Commercial Background | Textures, surfaces, backgrounds, negative-space compositions, and display scenes | Visible backdrop, texture, identifiable material, color, lighting, and composition |

Commercial Background mode does not make an ordinary image suitable for product display. Choose the mode that fits the visible content and check material and use-case claims yourself. Keep a batch consistent in its mode where possible.

Creator, Shoot location, and Date created on the home screen are optional. They are used when saving image metadata that supports those fields. Enter accurate information; these fields do not establish model releases, property releases, or rights clearance.

## 4. Models, endpoints, and API keys

### Cloud providers and relays

1. Select an image-capable model that your provider or relay actually supports. The current application's default preset is **OpenAI GPT-5.6 Terra**; a preset name does not guarantee availability on your endpoint.
2. Paste the corresponding token into **API Key**.
3. For a direct provider connection, you can normally leave **Base URL** blank to use the selected preset's endpoint. For a relay, enter the complete API address it supplies, such as `https://your-relay.example/v1`. Do not enter its account dashboard URL.
4. Click **Test Connection**. Check the reported model, route, and endpoint.
5. After a successful test, import and generate. The application also performs a connection preflight when starting a batch.

Connection tests may send a small image test request. Generation, visual review, repair, and network retries can also add API calls. Charges and availability depend on your chosen provider.

### Credential controls

| Control | Purpose |
| --- | --- |
| Save to Windows | Follow the prompts to store a key in the current Windows user's credential storage |
| Load saved key | Load a stored key for this session; complete Windows Hello / PIN verification when requested |
| Replace saved key | Update the stored key for the current provider |
| Clear session | Clear the key used by the current session; this does not delete its stored Windows copy |
| Delete saved key | Remove the stored Windows key for the current provider |
| Migrate legacy keys | Follow the migration flow only when the application identifies a legacy configuration key |

A pasted key that you have not saved is for the current session. To avoid pasting it again on later launches, use **Save to Windows**, then **Load saved key** when needed. System verification depends on the credential operation. Use the correct token for each provider or endpoint. Keep keys out of image text, exports, and shared screenshots.

### Local Ollama

1. Install and run Ollama on this computer or an accessible network computer with an image-capable model. The local preset in the current interface uses `llava`.
2. On the server computer, run `ollama pull llava` to download that preset's model. If Ollama is not already running as a service, start it with `ollama serve`.
3. Select **Local Ollama** in PixTagra. Enter the **Local Server** address: normally `http://localhost:11434` for this computer, or the reachable server address, such as `http://192.168.1.20:11434`.
4. Click **Test Connection**. The Ollama route does not require a cloud API key.

This option uses Ollama's native API, rather than an arbitrary local web page or generic `/v1` endpoint. A network server must already allow remote connections, and its port must be reachable through the network and firewall. Hardware requirements and speed depend on the model. The initial model download requires internet access and disk space.

## 5. Importing, generating, and cancelling

- The file picker accepts JPEG/JPG, PNG, WebP, and TIF/TIFF. **Import support does not imply image-copy saving support.** TIFF can be analyzed and used for metadata-file export, but safe saving of TIFF copies is not supported in this version. Safe saving of animated WebP is also unsupported.
- The default workspace limit is 500 images. The interface suggests 50–100 images per generation batch; reduce the batch size on slower hardware or models.
- Use image checkboxes, **Select all**, **Deselect**, or the status selectors under **Selection / retry** to choose generation targets.
- **Generate metadata** processes checked, unsaved records that are unprocessed or failed. To replace completed content, use **Regenerate** on its card. Review items can be handled through **Selection / retry**.
- **Visual review** adds calls to check generated content against the image. Turning it off can leave results marked for review because that check did not run. A sufficient keyword count alone does not make a record Ready.
- Progress includes connection preflight and processing. Wait for the current operation to finish before saving, exporting, or changing settings.
- **Cancel** stops new images from being dispatched and waits for active requests, retaining results that return. Requests already sent to the provider may still be charged, and cancellation may take time.
- When a batch ends, use **Add images**, check the new unprocessed records, and generate again without redoing completed images.

## 6. Quality states and editing

| State | Meaning and action |
| --- | --- |
| Unprocessed | No usable result yet; select it and generate |
| Ready | Current automated checks passed; still review the content before submission |
| Needs review | Text exists, but quality, review, or compliance issues remain; inspect and correct or retry |
| Failed | A request or processing step failed; check the connection, model, and source image before retrying |
| Saved | An image copy was written; this is not agency approval |
| Saved — needs review | A copy was written, but quality or compliance issues remain |
| Source changed / missing | The source path, content, or file state changed; reimport the correct source before continuing |

### Keyword acceptance

For current generated records, the final keyword policy is **at least 20, a target of 25–30, and a maximum of 35**. It is not a fixed requirement to produce exactly 35, and it is not a promise that every model response will pass. Fewer than 20 after removing duplicates or unsupported terms is still below the acceptance minimum. Do not add irrelevant words to reach a count.

Check that the main subject appears in the first ten terms; that materials, species, and objects are accurate; and that the list avoids repetitive synonyms, invisible stories, and invented brands or locations. Records can still require review after meeting the count.

### Editing controls

- Edit the title, description, and keywords directly on an image card. Separate keywords with semicolons, commas, or line breaks. An established multiword phrase can remain one keyword.
- **Expand editor** provides per-item addition, removal, deduplication, use-case ordering, and copying. Click **Apply** to apply that dialog's changes to the card.
- Check multiple images and click **Add keywords** to append shared terms. Add only terms that are accurate for every selected image.
- Use **View keyword check** under **Selection / retry** to inspect issues. Manual edits do not automatically mean that review has passed; check the resulting messages.
- **Regenerate** calls the model again. When manual edits are detected, choose **Only keywords**, **Regenerate all**, or **Keep manual fields**.
- **Undo generation** can restore the previous content when history is available and the record is unsaved. It does not undo files already written or refund API charges.

Finish editing before saving image copies. Saved cards lock their editors. To process one again, retain any text you need, remove that record, and reimport the original image.

## 7. Saving image copies

Expand **Save / Export** and use the **Image copies** controls.

| Action | Scope and output location |
| --- | --- |
| Save selected | Checked records; normally saved beside each source image |
| Save ready | Records passing the current quality checks across the workspace, regardless of checkboxes; normally saved beside their sources |
| Save all | Records across the workspace that have not failed processing; may include incomplete or review-needed metadata; normally saved beside their sources |
| Save to folder | Checked records; choose a parent directory and the application creates an `SEO_Ready_date_time` subfolder |

Saving creates new image copies and does not overwrite source images. Output filenames are normally based on titles, with collision avoidance. JPEG, PNG, and static WebP use their respective supported metadata-writing paths. Fields visible in other software can differ by format and viewer.

**Save selected, Save all, and Save to folder do not guarantee that every quality issue has been filtered out.** Use **Save ready** to save only records passing automated checks. Review warnings can remain after a successful write. The output folder name `SEO_Ready` is not a quality certification.

## 8. JSON and agency exports

### JSON

- **JSON** exports cached metadata records, including records that may be incomplete or need review, for retaining text and inspection details.
- **JSON ready** exports only records that currently pass quality checks.
- Neither embeds images nor filters by image checkboxes. These are not universal agency submission templates. The current interface has no button to import exported JSON back as a workspace.

### Agency destination menu

1. Expand **Save / Export** and choose a destination under **Metadata files**.
2. Read its hint. If the button shows **Export (blocked: …)**, correct the reported records first.
3. Click **Export**, respond to quality and rights-review prompts, and choose an output location. Alamy asks for a parent folder.

| Destination | What this version exports | How to use it |
| --- | --- | --- |
| Shutterstock · CSV | CSV with the destination's configured columns | This application requires English metadata for this route. Use matching image filenames and check categories and content flags in the submission portal |
| Adobe Stock · CSV | CSV with Filename, Title, Keywords, Category, and Releases | The application checks fields such as title and filename length. Confirm categories and release files in the portal; no release is created automatically |
| Alamy · IPTC JPEG | JPEG copies with English IPTC metadata, plus `metadata.json` | JPEG source images only, with no format conversion. The English title becomes the caption; the full description stays in JSON. Upload the JPEG copies |
| Getty/iStock · DeepMeta CSV | English CSV for the DeepMeta 4 workflow | Import through DeepMeta and confirm keyword matches. This is not a claim that the Getty portal directly accepts this CSV |
| VCG · Manual entry reference | Reference CSV with Chinese column headings | For manual entry, not a confirmed official upload template. The first five terms are suggested core keywords to confirm in the contributor portal |

These are the output workflows provided by the current application. Account access, portal import, and actual submission outcomes still need confirmation in your contributor workflow.

### Three important export details

- **Agency export does not filter by image checkboxes.** It checks workspace records with both a title and a description. One blocking error can prevent the whole export. To export a subset, first retain needed text or export JSON as a reference, then remove unrelated records from the workspace. Deselecting them does not exclude them from export.
- CSV uses the filenames of the imported images. Ordinary saved image copies may be renamed from their titles. If you upload those copies, make the CSV filenames match the actual uploaded files. Duplicate filenames from different folders also cause conflicts; resolve them and reimport first.
- Export does not automatically translate text or add keywords. Current generated records with fewer than 20 or more than 35 final keywords block agency export. Some softer warnings can be accepted to continue, but that does not clear the underlying issue.

## 9. Workspace recovery and files

PixTagra saves workspace records, including source paths, metadata, selections, and processing state. Use **Resume Session** on the home screen when available. **Home** itself does not clear the workspace.

Sources must remain at their original paths and must not be replaced. Recovery does not contain image bytes or restore an API key. Default settings and workspace files are under `%USERPROFILE%\.picture_seo_studio`, a directory name retained for compatibility. Do not delete it as disposable build output.

**Remove** removes a record from the workspace. **Clear** clears the current workspace. Neither deletes source images from disk. Save needed copies and text before using either action. Close the application normally; if it is waiting for active requests or writes, let them finish.

## 10. Troubleshooting

| Symptom | What to check |
| --- | --- |
| 401 / 403 | Match the provider, endpoint, token, and model permissions; do not mix keys from different relays |
| 404 / model not found | Confirm the endpoint path and model availability on that provider; a dropdown preset alone is not proof |
| Timeouts, 429, or server errors | Check network connectivity, provider status, and rate limits; reduce the batch size. Retrying can add calls |
| Local connection fails | Check that Ollama is running, `llava` is installed, the address and port are correct, and the server is reachable |
| Connection works but generation does not start | Select unprocessed or failed records. Check workspace locks, write permissions, and source files. Connection success does not establish workspace access |
| Too few keywords or Needs review | Inspect the review reasons and correct or regenerate from visible evidence; do not add unsupported filler |
| Existing text stays Chinese after choosing English | Language switching does not translate records; generate again with English selected |
| Export is disabled | Finish active operations and inspect the destination hint for language, count, title, filename, or duplicate-name errors |
| TIFF saving fails | Safe TIFF-copy saving is unsupported; retain the original and use JSON or an applicable CSV export |
| Cannot find saved images | Check the completion message. Ordinary saves are normally beside the sources; Save to folder uses an `SEO_Ready_…` subfolder under the selected parent |
| Saved text cannot be edited | Saved cards lock their editors. Retain needed text, remove the record, and reimport the original |

## 11. Before submitting

Check that the text matches the image, keywords are supported, and filenames match. Confirm categories, releases, editorial use, and AI disclosure where applicable in the destination platform. PixTagra assists with metadata and output files; you remain responsible for the final content review and submission.
