# Deal With It! :sunglasses:
A Python API for creating "Deal With It"-like Images

[![Maintainability](https://api.codeclimate.com/v1/badges/c605c377abc80d6e9a7b/maintainability)](https://codeclimate.com/github/ericovis/deal_with_it/maintainability)
[![Test Coverage](https://api.codeclimate.com/v1/badges/c605c377abc80d6e9a7b/test_coverage)](https://codeclimate.com/github/ericovis/deal_with_it/test_coverage)

Demo and docs available at https://deal-with-it.herokuapp.com/

## Prerequisites

Docker and Docker Compose are required in order to run the local environment.

## Running local

To run a local dev environment run:

```
docker-compose up
```

The app will be available at http://localhost:5000

## Running tests

```
docker-compose run web pytest
```

## Contributing

1. Fork this repo
2. Make changes
3. Open a PR

#### Some features you could build:

- Add the ability to create a animated GIF out of the original image
- Add more processors
- Add APIs for Slack integration
- Nonsense stuff ;D


## License

Created by [Eric Magalhães](https://emagalha.es) under the [MIT License](/LICENSE)
