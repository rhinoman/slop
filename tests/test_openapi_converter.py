"""Tests for OpenAPI to SLOP conversion"""

import pytest
from slop.schema_converter import (
    OpenApiConverter, SlopFunction, detect_schema_format
)


class TestSchemaFormatDetection:
    """Tests for detect_schema_format function"""

    def test_detect_openapi3(self):
        spec = {"openapi": "3.0.0", "paths": {}}
        assert detect_schema_format(spec) == "openapi"

    def test_detect_swagger2(self):
        spec = {"swagger": "2.0", "paths": {}}
        assert detect_schema_format(spec) == "swagger"

    def test_detect_openapi_by_paths(self):
        spec = {"paths": {"/users": {}}}
        assert detect_schema_format(spec) == "openapi"

    def test_detect_jsonschema(self):
        spec = {"type": "object", "properties": {}}
        assert detect_schema_format(spec) == "jsonschema"


class TestOpenApiConverter:
    """Tests for OpenApiConverter class"""

    def test_simple_get_endpoint(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test API"},
            "paths": {
                "/users/{id}": {
                    "get": {
                        "summary": "Get user",
                        "parameters": [{
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer", "minimum": 1}
                        }],
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/User"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "User": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer"},
                            "name": {"type": "string"}
                        }
                    }
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # Check function name
        assert "get-users-by-id" in output
        # Check parameter type
        assert "(Int 1 ..)" in output
        # Check precondition from minimum
        assert "(@pre (>= id 1))" in output
        # Check complexity tier
        assert ":complexity tier-1" in output

    def test_post_with_request_body(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/users": {
                    "post": {
                        "summary": "Create user",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/CreateUser"}
                                }
                            }
                        },
                        "responses": {
                            "201": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/User"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "User": {"type": "object", "properties": {"id": {"type": "integer"}}},
                    "CreateUser": {"type": "object", "properties": {"name": {"type": "string"}}}
                }
            }
        }

        output = OpenApiConverter(storage_mode='none').convert(spec)

        # Check function name uses 'create' prefix
        assert "create-users" in output
        # Check body parameter
        assert "(body (Ptr CreateUser))" in output
        # Check precondition for body
        assert "(@pre (!= body nil))" in output
        # Check context
        assert ":context (body)" in output

    def test_error_type_generation(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/users/{id}": {
                    "get": {
                        "parameters": [{
                            "name": "id",
                            "in": "path",
                            "schema": {"type": "integer"}
                        }],
                        "responses": {
                            "200": {"description": "OK"},
                            "400": {"description": "Bad request"},
                            "404": {"description": "Not found"},
                            "500": {"description": "Server error"}
                        }
                    }
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # Check ApiError enum is generated
        assert "(type ApiError" in output
        assert "bad-request" in output
        assert "not-found" in output
        assert "internal-error" in output

    def test_example_extraction(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/users/{id}": {
                    "get": {
                        "parameters": [{
                            "name": "id",
                            "in": "path",
                            "schema": {"type": "integer"},
                            "example": 42
                        }],
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"type": "object"},
                                        "example": {"id": 42, "name": "Alice"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # Check @example is generated
        assert "@example" in output
        assert "42" in output

    def test_postcondition_for_object_response(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/users/{id}": {
                    "get": {
                        "parameters": [{
                            "name": "id",
                            "in": "path",
                            "schema": {"type": "integer"}
                        }],
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/User"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "User": {"type": "object", "properties": {"id": {"type": "integer"}}}
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # Check postcondition for non-nil result
        assert "@post" in output
        assert "match $result" in output

    def test_postcondition_for_list_response(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/users": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/User"}
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "components": {
                "schemas": {
                    "User": {"type": "object", "properties": {"id": {"type": "integer"}}}
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # Check postcondition for list result uses len
        assert "@post" in output
        assert "(len list)" in output

    def test_operation_tier_assignment(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {
                "/users/{id}": {
                    "get": {
                        "parameters": [{"name": "id", "in": "path", "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "OK"}}
                    },
                    "put": {
                        "parameters": [{"name": "id", "in": "path", "schema": {"type": "integer"}}],
                        "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
                        "responses": {"200": {"description": "OK"}}
                    },
                    "delete": {
                        "parameters": [{"name": "id", "in": "path", "schema": {"type": "integer"}}],
                        "responses": {"204": {"description": "Deleted"}}
                    }
                },
                "/users": {
                    "get": {
                        "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
                        "responses": {"200": {"description": "OK"}}
                    }
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # GET by ID should be tier-1
        assert ":complexity tier-1" in output
        # PUT should be tier-3
        assert ":complexity tier-3" in output
        # DELETE should be tier-2
        assert ":complexity tier-2" in output

    def test_reuses_json_schema_converter(self):
        spec = {
            "openapi": "3.0.0",
            "info": {"title": "Test"},
            "paths": {},
            "components": {
                "schemas": {
                    "User": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "integer", "minimum": 1},
                            "name": {"type": "string", "minLength": 1, "maxLength": 100},
                            "age": {"type": "integer", "minimum": 0, "maximum": 150}
                        },
                        "required": ["id", "name"]
                    }
                }
            }
        }

        output = OpenApiConverter().convert(spec)

        # Check that component schema is converted with range types
        assert "(type User" in output
        assert "(Int 1 ..)" in output
        assert "(String 1 .. 100)" in output
        assert "(Option (Int 0 .. 150))" in output


class TestSlopFunctionOutput:
    """Tests for SlopFunction to_slop output"""

    def test_function_with_all_annotations(self):
        fn = SlopFunction(
            name="get-user",
            params=[("id", "(Int 1 ..)")],
            return_type="(Result User ApiError)",
            intent="Get user by ID",
            hole_prompt="Fetch user from storage",
            hole_tier="tier-1",
            context=["id"],
            preconditions=["(>= id 1)"],
            postconditions=["(match $result ((ok u) (!= u nil)) ((error _) true))"],
            examples=[("42", "(ok user-42)")]
        )

        output = fn.to_slop()

        assert "(fn get-user ((id (Int 1 ..)))" in output
        assert '(@intent "Get user by ID")' in output
        assert "(@spec (((Int 1 ..)) -> (Result User ApiError)))" in output
        assert "(@pre (>= id 1))" in output
        assert "(@post (match $result" in output
        assert "(@example 42 -> (ok user-42))" in output
        assert ':complexity tier-1' in output
        assert ':context (id)' in output

    def test_function_without_optional_annotations(self):
        fn = SlopFunction(
            name="simple-fn",
            params=[("x", "Int")],
            return_type="Int",
            intent="Simple function",
            hole_prompt="Do something",
            hole_tier="tier-1"
        )

        output = fn.to_slop()

        assert "(fn simple-fn ((x Int))" in output
        assert "@pre" not in output
        assert "@post" not in output
        assert "@example" not in output
        assert ":required" not in output


class TestStorageModes:
    """Tests for storage mode options"""

    def _get_petstore_spec(self):
        return {
            "openapi": "3.0.0",
            "info": {"title": "Pet Store"},
            "paths": {
                "/pets": {
                    "get": {
                        "summary": "List pets",
                        "responses": {"200": {"description": "OK"}}
                    },
                    "post": {
                        "summary": "Create pet",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/NewPet"}
                                }
                            }
                        },
                        "responses": {"201": {"description": "Created"}}
                    }
                },
                "/pets/{id}": {
                    "get": {
                        "summary": "Get pet",
                        "parameters": [{
                            "name": "id",
                            "in": "path",
                            "schema": {"type": "integer", "minimum": 1}
                        }],
                        "responses": {"200": {"description": "OK"}}
                    }
                }
            },
            "components": {
                "schemas": {
                    "Pet": {"type": "object", "properties": {"id": {"type": "integer"}}},
                    "NewPet": {"type": "object", "properties": {"name": {"type": "string"}}}
                }
            }
        }

    def test_stub_mode_generates_requires(self):
        spec = self._get_petstore_spec()
        output = OpenApiConverter(storage_mode='stub').convert(spec)

        # Check @requires block is generated
        assert "(@requires storage" in output
        assert ':prompt "Which storage approach for this API?"' in output
        assert ":options" in output
        # Check storage function signatures
        assert "state-get-pet" in output
        assert "state-list-pets" in output
        assert "state-insert-pet" in output
        # Check functions have state parameter
        assert "(state (Ptr State))" in output
        # Check must-use includes storage function
        assert "state-get-pet" in output

    def test_map_mode_generates_state_types(self):
        spec = self._get_petstore_spec()
        output = OpenApiConverter(storage_mode='map').convert(spec)

        # Check State type is generated
        assert "(type PetId (Int 1 ..))" in output
        assert "(type State (record" in output
        assert "(pets (Map PetId Pet))" in output
        assert "(next-pet-id PetId)" in output
        # Check state-new function
        assert "fn state-new" in output
        # Check CRUD functions
        assert "fn state-get-pet" in output
        assert "fn state-list-pets" in output
        assert "fn state-insert-pet" in output
        assert "fn state-delete-pet" in output
        # No @requires block in map mode
        assert "(@requires storage" not in output

    def test_none_mode_no_storage_context(self):
        spec = self._get_petstore_spec()
        output = OpenApiConverter(storage_mode='none').convert(spec)

        # No @requires block
        assert "(@requires" not in output
        # No state type
        assert "(type State" not in output
        # No state parameter in functions
        assert "(state (Ptr State))" not in output
        # Functions just have their regular parameters
        assert "(fn get-pets-by-id ((id (Int 1 ..)))" in output

    def test_stub_mode_is_default(self):
        spec = self._get_petstore_spec()
        output = OpenApiConverter().convert(spec)

        # Default should be stub mode
        assert "(@requires storage" in output
