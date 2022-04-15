'use strict'

hljs.initHighlightingOnLoad();

const toBase64 = file => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.readAsDataURL(file);
    reader.onload = () => resolve(reader.result);
    reader.onerror = error => reject(error);
});

async function getData() {
    let data = {
        url: null,
        image: null
    };
    let form = document.getElementById('form');

    if (form.elements.url.value) {
        data.url = form.elements.url.value
    } else if (form.elements.image.files[0]) {
        data.image = await toBase64(form.elements.image.files[0]);
    }
    return data;
}

function isLoading() {
    let form = document.getElementById('form');
    let loading = document.getElementById('loading');
    let message = document.getElementById('message');
    let result = document.getElementById('result');
    
    form.style.display = 'none';
    loading.style.display = 'block';
    result.style.display = 'none';
    setMessage(null)
}

function loaded() {
    let form = document.getElementById('form');
    let loading = document.getElementById('loading');
    let result = document.getElementById('result');

    form.style.display = 'block';
    loading.style.display = 'none';
    result.style.display = 'block';
}

function setMessage(msg) {
    let message = document.getElementById('message');
    message.innerHTML = msg;
}


async function onSubmit() {   
    isLoading();
    let data = await getData();

    if (!data.url && !data.image) {        
        form.style.display = 'block';
        setMessage('No image or URL was provided!')
        result.style.display = 'block';
        loaded();
        return;
    }

    fetch('/api', {
        method: 'POST',
        cache: 'no-cache',
        headers: {
        'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    }).then(
        function(response) {
        if (response.status !== 200) {
            response.json().then(function(data) {
                setMessage(data.message);
                
            });            
        } else {
            response.json().then(function(data) {
                let img = document.getElementById('result-img');
                let out = JSON.parse(data);
                img.src = out.image;
                img.style.display = 'block';                        
            });
        }
        loaded();
        return;
        }
    )
    .catch(function(err) {
        console.log('Fetch Error :-S', err);
    });
}
