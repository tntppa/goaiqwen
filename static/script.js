const fileInput = document.getElementById('file-input');
const uploadArea = document.getElementById('upload-area');
const previewWrap = document.getElementById('preview-wrap');
const preview = document.getElementById('preview');
const pdfInfo = document.getElementById('pdf-info');
const submitBtn = document.getElementById('submit-btn');
const resultWrap = document.getElementById('result-wrap');
const resultText = document.getElementById('result-text');

let selectedFile = null;

function getExt(name) {
    return name.split('.').pop().toLowerCase();
}

function showFilePreview(file) {
    const ext = getExt(file.name);
    previewWrap.style.display = 'none';
    pdfInfo.style.display = 'none';

    if (['jpg', 'jpeg', 'png'].includes(ext)) {
        const reader = new FileReader();
        reader.onload = (e) => {
            preview.src = e.target.result;
            previewWrap.style.display = 'block';
        };
        reader.readAsDataURL(file);
    } else if (ext === 'pdf') {
        pdfInfo.style.display = 'block';
        pdfInfo.innerHTML = '<div class="pdf-badge">&#128209; ' + file.name + '</div>';
    } else if (['xls', 'xlsx'].includes(ext)) {
        pdfInfo.style.display = 'block';
        pdfInfo.innerHTML = '<div class="pdf-badge">&#128202; ' + file.name + '</div>';
    } else if (['doc', 'docx'].includes(ext)) {
        pdfInfo.style.display = 'block';
        pdfInfo.innerHTML = '<div class="pdf-badge">&#128221; ' + file.name + '</div>';
    }
}

function setFile(file) {
    selectedFile = file;
    showFilePreview(file);
    submitBtn.disabled = false;
    resultWrap.style.display = 'none';
    resultText.textContent = '';
}

uploadArea.addEventListener('dragover', function(e) {
    e.preventDefault();
    uploadArea.classList.add('dragover');
});

uploadArea.addEventListener('dragleave', function() {
    uploadArea.classList.remove('dragover');
});

uploadArea.addEventListener('drop', function(e) {
    e.preventDefault();
    uploadArea.classList.remove('dragover');
    if (e.dataTransfer.files.length) {
        setFile(e.dataTransfer.files[0]);
    }
});

fileInput.addEventListener('change', function() {
    if (fileInput.files.length) {
        setFile(fileInput.files[0]);
    }
    fileInput.value = '';
});

submitBtn.addEventListener('click', async function() {
    if (!selectedFile) {
        return;
    }

    submitBtn.disabled = true;
    submitBtn.innerHTML = '<span class="loading"></span>处理中，请稍候...';
    resultWrap.style.display = 'none';
    resultText.textContent = '';
    document.getElementById('elapsed-time').textContent = '';

    const formData = new FormData();
    formData.append('file', selectedFile);

    const t0 = performance.now();
    try {
        const resp = await fetch('/analyze', { method: 'POST', body: formData });
        const data = await resp.json();
        const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
        resultWrap.style.display = 'block';
        document.getElementById('elapsed-time').textContent = '用时 ' + elapsed + ' 秒';
        if (data.error) {
            resultText.innerHTML = '<span class="error">错误：' + data.error + '</span>';
        } else {
            resultText.textContent = data.result;
        }
    } catch (err) {
        const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
        resultWrap.style.display = 'block';
        document.getElementById('elapsed-time').textContent = '用时 ' + elapsed + ' 秒';
        resultText.innerHTML = '<span class="error">请求失败，请检查服务是否正常运行。</span>';
    } finally {
        submitBtn.disabled = false;
        submitBtn.innerHTML = '上传并分析';
    }
});
