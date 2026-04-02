function submitForm() {
  const wm   = wmInput.files;
  const vids = vidInput.files;
  if (!wm.length || !vids.length) { toast('Select files first', true); return; }

  const formData = new FormData();
  formData.append('watermark', wm[0]);
  Array.from(vids).forEach(f => formData.append('videos', f));

  const btn = document.getElementById('submit-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Uploading Batch...';

  fetch('/process', { method: 'POST', body: formData })
    .then(response => response.json()) // We now expect a JSON object
    .then(data => {
        if (data.redirect_url) {
            window.location.href = data.redirect_url;
        } else {
            toast('Upload failed', true);
            btn.disabled = false;
        }
    })
    .catch(e => { 
        toast('Upload error: ' + e, true); 
        btn.disabled = false; 
    });
}
