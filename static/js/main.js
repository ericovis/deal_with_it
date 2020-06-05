'use strict'

hljs.initHighlightingOnLoad();

function onSubmit() {
    console.log('Calling the API');
    let form = document.getElementById('form');
    let result = document.getElementById('result');
    let data = {};
    
    form.style.display = 'none';

    if (form.elements.url.value) {
        data.url = form.elements.url.value
    } else if (form.elements.image.value) {
        data.image = form.elements.image.value
    } else {
        console.log('Form invalid!');
        form.style.display = 'block';
        return;
    }

    console.log(data)
    form.style.display = 'none';
    fetch('/api', {
        method: 'POST',
        cache: 'no-cache',
        headers: {
        'Content-Type': 'application/json'
        },
        body: JSON.stringify(data)
    })
}
