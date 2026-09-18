import httpx, json, uuid, sys

ENGINE = "http://localhost:8000"
PK_DEMO = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
PK_KETTLE = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"

def client(pk):
    return httpx.Client(base_url=ENGINE, headers={"Authorization": f"Bearer {pk}"}, timeout=30)

def create_cart(c, conn):
    r = c.post(f"/api/shop/{conn}/cart")
    r.raise_for_status()
    return r.json()["cart_id"]

def get_cart(c, conn, cart_id):
    r = c.get(f"/api/shop/{conn}/cart/{cart_id}")
    r.raise_for_status()
    return r.json()

def act(c, conn, session, product_id, variant_id=None, cart_id=None, said="Add that to my cart"):
    body = {"connection_id": conn, "session_id": session, "product_id": product_id, "said": said}
    if variant_id: body["variant_id"] = variant_id
    if cart_id: body["cart_id"] = cart_id
    return c.post("/api/chat/act", json=body)

def chat(c, conn, session, message, cart_id=None):
    body = {"connection_id": conn, "session_id": session, "message": message}
    if cart_id: body["cart_id"] = cart_id
    return c.post("/api/chat", json=body)

def line_qty_for_variant(cart, variant_id):
    for line in cart.get("lines", []):
        if line.get("variant_id") == variant_id or line.get("variant_ref") == variant_id:
            return line.get("quantity")
    return None

def dump_lines(cart):
    return [(l.get("product_id"), l.get("variant_id") or l.get("variant_ref"), l.get("quantity")) for l in cart.get("lines", [])]

if __name__ == "__main__":
    cmd = sys.argv[1]

    if cmd == "reAdd":
        c = client(PK_DEMO)
        session = "sess-" + uuid.uuid4().hex[:8]
        cart_id = create_cart(c, "conn_demo")
        print("cart", cart_id)

        r = act(c, "conn_demo", session, "P1001", "P1001-9", cart_id=cart_id, said="Add the Trailblazer Running Shoe size 9 to my cart")
        print("initial add:", r.status_code, r.json().get("reply"))
        cart = get_cart(c, "conn_demo", cart_id)
        print("cart after initial add:", dump_lines(cart))

        phrasings = [
            "add another one of those",
            "add the trailblazer again",
            "can you add another trailblazer running shoe",
            "I'd like one more of the same shoe",
            "add it again please",
            "throw in another pair of those",
            "one more trailblazer, same size",
            "add another of the trailblazer shoes",
            "give me one more of that",
            "add that shoe again to my basket",
        ]
        for i, p in enumerate(phrasings, 1):
            r = chat(c, "conn_demo", session, p, cart_id=cart_id)
            reply = r.json()
            cart = get_cart(c, "conn_demo", cart_id)
            qty = line_qty_for_variant(cart, "P1001-9")
            print(f"[{i}] '{p}' -> action={reply.get('selected_action')} reply={reply.get('reply')[:80]!r} lines={dump_lines(cart)} qty9={qty}")

    elif cmd == "differentProduct":
        c = client(PK_DEMO)
        session = "sess-" + uuid.uuid4().hex[:8]
        cart_id = create_cart(c, "conn_demo")
        r = act(c, "conn_demo", session, "P1001", "P1001-9", cart_id=cart_id, said="Add the Trailblazer size 9")
        print("initial:", r.json().get("reply"))
        r = chat(c, "conn_demo", session, "actually add another product, the P1005 one, a different item entirely", cart_id=cart_id)
        cart = get_cart(c, "conn_demo", cart_id)
        print("after 'another' but new product request:", r.json().get("reply")[:150])
        print("lines:", dump_lines(cart))
        # more natural phrasing referencing a genuinely different product by name
        session2 = "sess-" + uuid.uuid4().hex[:8]
        cart_id2 = create_cart(c, "conn_demo")
        act(c, "conn_demo", session2, "P1001", "P1001-9", cart_id=cart_id2, said="Add the Trailblazer size 9")
        r = chat(c, "conn_demo", session2, "can you also add another running shoe, something different from the trailblazer, maybe a trail shoe", cart_id=cart_id2)
        cart2 = get_cart(c, "conn_demo", cart_id2)
        print("2nd variant phrasing reply:", r.json().get("reply")[:150])
        print("lines2:", dump_lines(cart2))

    elif cmd == "notInCart":
        c = client(PK_DEMO)
        session = "sess-" + uuid.uuid4().hex[:8]
        cart_id = create_cart(c, "conn_demo")
        r = chat(c, "conn_demo", session, "add another one of the trailblazer shoes", cart_id=cart_id)
        print("reply:", r.status_code, r.json().get("reply"))
        cart = get_cart(c, "conn_demo", cart_id)
        print("lines:", dump_lines(cart))

    elif cmd == "kettleDecline":
        c = client(PK_KETTLE)
        session = "sess-" + uuid.uuid4().hex[:8]
        cart_id = create_cart(c, "conn_kettle")
        queries = [
            "do you sell fresh sushi rolls?",
            "can I get a bicycle repair kit here?",
            "do you have printer ink cartridges?",
            "do I eat gluten-free bread from you guys?",
            "can I get motor oil for my car?",
        ]
        for i, q in enumerate(queries, 1):
            r = chat(c, "conn_kettle", session, q, cart_id=cart_id)
            print(f"[{i}] '{q}' -> {r.json().get('reply')[:150]!r}")

    elif cmd == "oatMilk":
        c = client(PK_KETTLE)
        for i in range(6):
            session = "sess-" + uuid.uuid4().hex[:8]
            cart_id = create_cart(c, "conn_kettle")
            r = chat(c, "conn_kettle", session, "do you have oat milk?", cart_id=cart_id)
            status = r.status_code
            try:
                reply = r.json().get('reply')
            except Exception:
                reply = r.text
            print(f"[{i+1}] status={status} reply={reply!r:.150}")

    elif cmd == "tasteRegress":
        c = client(PK_KETTLE)
        queries = [
            "I want something smooth and not too bitter",
            "what's your strongest, boldest coffee?",
            "do you have a light roast that's fruity?",
            "I like something sweet, not acidic",
            "what would you recommend for someone who dislikes bitter coffee?",
        ]
        for i, q in enumerate(queries, 1):
            session = "sess-" + uuid.uuid4().hex[:8]
            cart_id = create_cart(c, "conn_kettle")
            r = chat(c, "conn_kettle", session, q, cart_id=cart_id)
            print(f"[{i}] '{q}' -> {r.json().get('reply')[:200]!r}")

    elif cmd == "genericVerbCoffee":
        c = client(PK_KETTLE)
        queries = [
            "what do you have to drink that's decaf?",
            "I want to get a latte to go",
            "can I have an iced coffee please",
            "what can I eat alongside my coffee, like a pastry?",
        ]
        for i, q in enumerate(queries, 1):
            session = "sess-" + uuid.uuid4().hex[:8]
            cart_id = create_cart(c, "conn_kettle")
            r = chat(c, "conn_kettle", session, q, cart_id=cart_id)
            print(f"[{i}] '{q}' -> {r.json().get('reply')[:200]!r}")
