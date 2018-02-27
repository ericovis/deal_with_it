# Deal With It! :sunglasses:
A Python API for creating "Deal With It"-like GIFs


## Prerequisites

Docker and Docker Compose are required in order to run the local environment or deploy this project on AWS Lambda. All the other dependencies are already configured inside the containers described in this project.

## Running local

To run a local dev environment first build the containers with:

```
docker-compose build
```

Then start the local environment by running:

```
docker-compose up
```

The Web application will be available at [http://localhost:8080](http://localhost:8080) and the API at [http://localhost:5000](http://localhost:5000)


## Deploying the API on AWS Lambda

- Install the NPM dependencies described at the *package.json* file on [/api/package.json](/api/package.json)
- Configure your local computer with your AWS credentials
- Run `sls deploy` from the */api* folder
- While creating the deployment package add the following code to *wsgi.py* and save the file:

``` python
try:
  import unzip_requirements
except ImportError:
  pass
```

## Roadmap

- Create separated APIs for Slack, Web, and Twitter
- Improve the AWS Lambda deployment process
- Create automated deployment (Web + API)
- Enable face analysis with Amazon Rekognition


## License

Created by [Eric Magalhães](https://emagalha.es) under the [MIT License](/LICENSE)
